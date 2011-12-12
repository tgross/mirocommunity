# Copyright 2009 - Participatory Culture Foundation
# 
# This file is part of Miro Community.
# 
# Miro Community is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
# 
# Miro Community is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with Miro Community.  If not, see <http://www.gnu.org/licenses/>.

import datetime
from email.utils import formatdate
import httplib
import logging
import mimetypes
import re
import time
import urllib
import urllib2

from BeautifulSoup import BeautifulSoup
from django.conf import settings
from django.contrib.comments.moderation import CommentModerator, moderator
from django.contrib.sites.models import Site
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.base import ContentFile
from django.core.mail import EmailMessage
from django.core.validators import ipv4_re
from django.db import models
from django.template import loader, Context
from django.template.defaultfilters import slugify
from notification import models as notification
import tagging
import vidscraper

from localtv.exceptions import CannotOpenImageUrl
from localtv.models.base import (VIDEO_SERVICE_REGEXES, Thumbnailable,
                                 THUMB_SIZES, StatusedThumbnailable,
                                 StatusedThumbnailableQuerySet,
                                 StatusedThumbnailableManager)
from localtv.models.fields import BitLyWrappingURLField
from localtv.models.settings import SiteLocation
from localtv.settings import ENABLE_ORIGINAL_VIDEO, voting_enabled
from localtv.signals import submit_finished, post_video_from_vidscraper
from localtv.templatetags.filters import sanitize
from localtv.utils import (unicode_set, normalize_newlines, hash_file_obj,
                           quote_unicode_url, send_notice)


EMPTY = object()


class VideoBase(models.Model):
    """
    Base class between Video and OriginalVideo.  It would be simple enough to
    duplicate these fields, but this way it's easier to add more points of
    duplication in the future.
    """
    name = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    thumbnail_url = models.URLField(
        verify_exists=False, blank=True, max_length=400)

    class Meta:
        abstract = True

class OriginalVideo(VideoBase):

    VIDEO_ACTIVE, VIDEO_DELETED, VIDEO_DELETE_PENDING = range(3)

    video = models.OneToOneField('Video', related_name='original')
    thumbnail_updated = models.DateTimeField(blank=True)
    remote_video_was_deleted = models.IntegerField(default=VIDEO_ACTIVE)
    remote_thumbnail_hash = models.CharField(max_length=64, default='')

    class Meta:
        app_label = 'localtv'

    def changed_fields(self, override_vidscraper_result=None):
        """
        Check our video for new data.
        """
        video = self.video
        if not video.website_url:
            # we shouldn't have been created, but either way we can't do
            # anything here
            self.delete()
            return {}

        remote_video_was_deleted = False
        fields = ['title', 'description', 'tags', 'thumbnail_url']
        if override_vidscraper_result is not None:
            vidscraper_video = override_vidscraper_result
        else:
            try:
                vidscraper_video = vidscraper.auto_scrape(
                    video.website_url, fields=fields)
            except vidscraper.errors.VideoDeleted:
                remote_video_was_deleted = True

        # Now that we have the "scraped_data", analyze it: does it look like
        # a skeletal video, with no data? Then we infer it was deleted.
        if remote_video_was_deleted or all(not getattr(vidscraper_video, field)
                                           for field in fields):
            remote_video_was_deleted = True
        # If the scraped_data has all None values, then infer that the remote video was
        # deleted.

        if remote_video_was_deleted:
            if self.remote_video_was_deleted == OriginalVideo.VIDEO_DELETED:
                return {} # We already notified the admins of the deletion.
            else:
                return {'deleted': True}
        elif self.remote_video_was_deleted:
            return {'deleted': False}

        changed_fields = {}

        for field in fields:
            if field == 'tags': # special case tag checking
                if vidscraper_video.tags is None:
                    # failed to get tags, so don't send a spurious change
                    # message
                    continue
                new = unicode_set(vidscraper_video.tags)
                if getattr(settings, 'FORCE_LOWERCASE_TAGS'):
                    new = unicode_set(name.lower() for name in new)
                old = unicode_set(self.tags)
                if new != old:
                    changed_fields[field] = new
            elif field == 'thumbnail_url':
                if vidscraper_video.thumbnail_url != self.thumbnail_url:
                    changed_fields[field] = vidscraper_video.thumbnail_url
                else:
                    right_now = datetime.datetime.utcnow()
                    if self._remote_thumbnail_appears_changed():
                        changed_fields['thumbnail_updated'] = right_now
            else:
                if field == 'title':
                    model_field = 'name'
                else:
                    model_field = field
                if (normalize_newlines(
                        getattr(vidscraper_video, field)) !=
                    normalize_newlines(
                        getattr(self, model_field))):
                    changed_fields[model_field] = getattr(vidscraper_video, field)

        return changed_fields

    def originals_for_changed_fields(self, changed_fields):
        '''The OriginalVideo emails need to say not just the new data, but also
        provide the value that was in the OriginalVideo object just before the
        email is sent.

        This function takes a changed_fields dictionary, and uses its keys to
        figure out what relevant snapshotted information would help the user
        contextualize the changed_fields data.'''
        old_fields = {}

        if 'deleted' in changed_fields:
            return old_fields

        for key in changed_fields:
            old_fields[key] = getattr(self, key)

        return old_fields

    def _remote_thumbnail_appears_changed(self):
        '''This private method checks if the remote thumbnail has been updated.

        It takes no arguments, because you are only supposed to call it
        when the remote video service did not give us a new thumbnail URL.

        It returns a boolean. True, if and only if the remote video has:

        * a Last-Modified header indicating it has been modified, and
        * HTTP response body that hashes to a different SHA1 than the
          one we stored.

        It treats "self" as read-only.'''
        # because the data might have changed, check to see if the
        # thumbnail has been modified
        made_time = time.mktime(self.thumbnail_updated.utctimetuple())
        # we take made_time literally, because the localtv app MUST
        # be storing UTC time data in the column.
        modified = formatdate(made_time,
                                          usegmt=True)
        request = urllib2.Request(self.thumbnail_url)
        request.add_header('If-Modified-Since', modified)
        try:
            response = urllib2.build_opener().open(request)
        except urllib2.HTTPError:
            # We get this for 304, but we'll just ignore all the other
            # errors too
            return False
        else:
            if response.info().get('Last-modified', modified) == \
                    modified:
                # hasn't really changed, or doesn't exist
                return False

        # If we get here, then the remote server thinks that the file is fresh.
        # We should check its SHA1 hash against the one we have stored.
        new_sha1 = hash_file_obj(response)

        if new_sha1 == self.remote_thumbnail_hash:
            # FIXME: Somehow alert downstream layers that it is safe to update
            # the modified-date in the database.
            return False # bail out early, empty -- the image is the same

        # Okay, so the hashes do not match; the remote image truly has changed.
        # Let's report the timestamp as having a Last-Modified date of right now.
        return True

    def send_deleted_notification(self):
        if self.remote_video_was_deleted == OriginalVideo.VIDEO_DELETE_PENDING:
            from localtv.utils import send_notice
            t = loader.get_template('localtv/admin/video_deleted.txt')
            c = Context({'video': self.video})
            subject = '[%s] Video Deleted: "%s"' % (
                self.video.site.name, self.video.name)
            message = t.render(c)
            send_notice('admin_video_updated', subject, message,
                        sitelocation=SiteLocation.objects.get(
                    site=self.video.site))
            # Update the OriginalVideo to show that we sent this notification
            # out.
            self.remote_video_was_deleted = OriginalVideo.VIDEO_DELETED
        else:
            # send the message next time
            self.remote_video_was_deleted = OriginalVideo.VIDEO_DELETE_PENDING
        self.save()

    def update(self, override_vidscraper_result = None):
        from localtv.utils import get_or_create_tags

        changed_fields = self.changed_fields(override_vidscraper_result)
        if not changed_fields:
            return # don't need to do anything

        # Was the remote video deleted?
        if changed_fields.pop('deleted', None):
            # Have we already sent the notification
            # Mark inside the OriginalVideo that the video has been deleted.
            # Yes? Uh oh.
            self.send_deleted_notification()
            return # Stop processing here.

        original_values = self.originals_for_changed_fields(changed_fields)

        changed_model = False
        for field in changed_fields.copy():
            if field == 'tags': # special case tag equality
                if set(self.tags) == set(self.video.tags):
                    self.tags = self.video.tags = get_or_create_tags(
                        changed_fields.pop('tags'))
            elif field in ('thumbnail_url', 'thumbnail_updated'):
                if self.thumbnail_url == self.video.thumbnail_url:
                    value = changed_fields.pop(field)
                    if field == 'thumbnail_url':
                        self.thumbnail_url = self.video.thumbnail_url = value
                    changed_model = True
                    self.video.save_thumbnail()
            elif getattr(self, field) == getattr(self.video, field):
                value = changed_fields.pop(field)
                setattr(self, field, value)
                setattr(self.video, field, value)
                changed_model = True

        if self.remote_video_was_deleted:
            self.remote_video_was_deleted = OriginalVideo.VIDEO_ACTIVE
            changed_model = True

        if changed_model:
            self.save()
            self.video.save()

        if not changed_fields: # modified them all
            return

        self.send_updated_notification(changed_fields, original_values)

    def send_updated_notification(self, changed_fields, originals_for_changed_fields):
        from localtv.utils import send_notice, get_or_create_tags

        # Create a custom hodge-podge of changed fields and the original values
        hodge_podge = {}
        for key in changed_fields:
            hodge_podge[key] = (
                changed_fields[key],
                originals_for_changed_fields.get(key, None))

        t = loader.get_template('localtv/admin/video_updated.txt')
        c = Context({'video': self.video,
                     'original': self,
                     'changed_fields': hodge_podge})
        subject = '[%s] Video Updated: "%s"' % (
            self.video.site.name, self.video.name)
        message = t.render(c)
        send_notice('admin_video_updated', subject, message,
                    sitelocation=SiteLocation.objects.get(
                site=self.video.site))

        # And update the self instance to reflect the changes.
        for field in changed_fields:
            if field == 'tags':
                self.tags = get_or_create_tags(changed_fields[field])
            else:
                setattr(self, field, changed_fields[field])
        self.save()


class VideoQuerySet(StatusedThumbnailableQuerySet):

    def with_best_date(self, use_original_date=True):
        if use_original_date:
            published = 'localtv_video.when_published,'
        else:
            published = ''
        return self.extra(select={'best_date': """
COALESCE(%slocaltv_video.when_approved,
localtv_video.when_submitted)""" % published})

    def with_watch_count(self, since=EMPTY):
        """
        Returns a QuerySet of videos annotated with a ``watch_count`` of all
        watches since ``since`` (a datetime, which defaults to seven days ago).
        """
        if since is EMPTY:
            since = datetime.datetime.now() - datetime.timedelta(days=7)

        return self.extra(
            select={'watch_count': """SELECT COUNT(*) FROM localtv_watch
WHERE localtv_video.id = localtv_watch.video_id AND
localtv_watch.timestamp > %s"""},
            select_params = (since,)
        )


class VideoManager(StatusedThumbnailableManager):

    def get_query_set(self):
        return VideoQuerySet(self.model, using=self._db)

    def with_best_date(self, *args, **kwargs):
        return self.get_query_set().with_best_date(*args, **kwargs)

    def popular_since(self, *args, **kwargs):
        return self.get_query_set().popular_since(*args, **kwargs)

    def get_sitelocation_videos(self, sitelocation=None):
        """
        Returns a QuerySet of videos which are active and tied to the
        sitelocation. This QuerySet is cached on the request.
        
        """
        if sitelocation is None:
            sitelocation = SiteLocation.objects.get_current()
        return self.active().filter(site=sitelocation.site)

    def get_featured_videos(self, sitelocation=None):
        """
        Returns a ``QuerySet`` of active videos which are considered "featured"
        for the sitelocation.

        """
        return self.get_sitelocation_videos(sitelocation).filter(
            last_featured__isnull=False
        ).order_by(
            '-last_featured',
            '-when_approved',
            '-when_published',
            '-when_submitted'
        )

    def get_latest_videos(self, sitelocation=None):
        """
        Returns a ``QuerySet`` of active videos for the sitelocation, ordered by
        decreasing ``best_date``.
        
        """
        if sitelocation is None:
            sitelocation = SiteLocation.objects.get_current()
        return self.get_sitelocation_videos(sitelocation).with_best_date(
            sitelocation.use_original_date
        ).order_by('-best_date')

    def get_popular_videos(self, sitelocation=None):
        """
        Returns a ``QuerySet`` of active videos considered "popular" for the
        current sitelocation.

        """
        return self.get_latest_videos(sitelocation).with_watch_count().order_by(
            '-watch_count',
            '-best_date'
        )

    def get_category_videos(self, category, sitelocation=None):
        """
        Returns a ``QuerySet`` of active videos considered part of the selected
        category or its descendants for the sitelocation.

        """
        if sitelocation is None:
            sitelocation = SiteLocation.objects.get_current()
        # category.approved_set already checks active().
        return category.approved_set.filter(
            site=sitelocation.site
        ).with_best_date(
            sitelocation.use_original_date
        ).order_by('-best_date')

    def get_tag_videos(self, tag, sitelocation=None):
        """
        Returns a ``QuerySet`` of active videos with the given tag for the
        sitelocation.

        """
        if sitelocation is None:
            sitelocation = SiteLocation.objects.get_current()
        return Video.tagged.with_all(tag).active().filter(
            site=sitelocation.site
        ).order_by(
            '-when_approved',
            '-when_published',
            '-when_submitted'
        )

    def get_author_videos(self, author, sitelocation=None):
        """
        Returns a ``QuerySet`` of active videos published or produced by
        ``author`` related to the sitelocation.

        """
        return self.get_latest_videos(sitelocation).filter(
            models.Q(authors=author) | models.Q(user=author)
        ).distinct().order_by('-best_date')

    def in_feed_order(self, feed=None, sitelocation=None):
        """
        Returns a ``QuerySet`` of active videos ordered by the order they were
        in when originally imported.
        """
        if sitelocation is None and feed:
            sitelocation = SiteLocation.objects.get(site=feed.site)
        if sitelocation:
            qs = self.get_latest_videos(sitelocation)
        else:
            qs = self.all()
        if feed:
            qs = qs.filter(feed=feed)
        return qs.order_by('-feedimportindex__source_import__start',
                           'feedimportindex__index',
                           '-id')


class Video(Thumbnailable, VideoBase, StatusedThumbnailable):
    """
    Fields:
     - name: Name of this video
     - site: Site this video is attached to
     - description: Video description
     - tags: A list of Tag objects associated with this item
     - categories: Similar to Tags
     - authors: the person/people responsible for this video
     - file_url: The file this object points to (if any) ... if not
       provided, at minimum we need the embed_code for the item.
     - file_url_length: size of the file, in bytes
     - file_url_mimetype: mimetype of the file
     - when_submitted: When this item was first entered into the
       database
     - when_approved: When this item was marked to appear publicly on
       the site
     - when_published: When this file was published at its original
       source (if known)
     - last_featured: last time this item was featured.
     - status: one of Video.STATUS_CHOICES
     - feed: which feed this item came from (if any)
     - website_url: The page that this item is associated with.
     - embed_code: code used to embed this item.
     - flash_enclosure_url: Crappy enclosure link that doesn't
       actually point to a url.. the kind crappy flash video sites
       give out when they don't actually want their enclosures to
       point to video files.
     - guid: data used to identify this video
     - has_thumbnail: whether or not this video has a thumbnail
     - thumbnail_url: url to the thumbnail, if such a thing exists
     - thumbnail_extension: extension of the *internal* thumbnail, saved on the
       server (usually paired with the id, so we can determine "1123.jpg" or
       "1186.png"
     - user: if not None, the user who submitted this video
     - search: if not None, the SavedSearch from which this video came
     - video_service_user: if not blank, the username of the user on the video
       service who owns this video.  We can figure out the service from the
       website_url.
     - contact: a free-text field for anonymous users to specify some contact
       info
     - notes: a free-text field to add notes about the video
    """
    site = models.ForeignKey(Site)
    categories = models.ManyToManyField('localtv.Category', blank=True)
    authors = models.ManyToManyField('auth.User', blank=True,
                                     related_name='authored_set')
    file_url = BitLyWrappingURLField(verify_exists=False, blank=True)
    file_url_length = models.IntegerField(null=True, blank=True)
    file_url_mimetype = models.CharField(max_length=60, blank=True)
    when_modified = models.DateTimeField(auto_now=True,
                                         db_index=True,
                                         default=datetime.datetime.now)
    when_submitted = models.DateTimeField(auto_now_add=True)
    when_approved = models.DateTimeField(null=True, blank=True)
    when_published = models.DateTimeField(null=True, blank=True)
    last_featured = models.DateTimeField(null=True, blank=True)
    feed = models.ForeignKey('localtv.Feed', null=True, blank=True)
    website_url = BitLyWrappingURLField(verbose_name='Website URL',
                                        verify_exists=False,
                                        blank=True)
    embed_code = models.TextField(blank=True)
    flash_enclosure_url = BitLyWrappingURLField(verify_exists=False,
                                                blank=True)
    guid = models.CharField(max_length=250, blank=True)
    user = models.ForeignKey('auth.User', null=True, blank=True)
    search = models.ForeignKey('localtv.SavedSearch', null=True, blank=True)
    video_service_user = models.CharField(max_length=250, blank=True,
                                          default='')
    video_service_url = models.URLField(verify_exists=False, blank=True,
                                        default='')
    contact = models.CharField(max_length=250, blank=True,
                               default='')
    notes = models.TextField(blank=True)
    calculated_source_type = models.CharField(max_length=255, blank=True, default='')

    objects = VideoManager()

    THUMB_SIZES = THUMB_SIZES

    class Meta:
        ordering = ['-when_submitted']
        get_latest_by = 'when_modified'
        app_label = 'localtv'

    def __unicode__(self):
        return self.name

    @models.permalink
    def get_absolute_url(self):
        return ('localtv_view_video', (),
                {'video_id': self.id,
                 'slug': slugify(self.name)[:30]})

    @classmethod
    def from_vidscraper_video(cls, video, status=None, commit=True,
                              using='default', source_import=None, **kwargs):
        """
        Builds a :class:`Video` instance from a
        :class:`vidscraper.suites.base.Video` instance. If `commit` is False,
        the :class:`Video` will not be saved.  There will be a `save_m2m()`
        method that must be called after you call `save()`.

        """
        if not video.embed_code and not video.file_url:
            raise InvalidVideo

        if status is None:
            status = cls.UNAPPROVED
        if 'site_id' not in kwargs:
            kwargs['site_id'] = settings.SITE_ID

        authors = kwargs.pop('authors', None)
        categories = kwargs.pop('categories', None)

        now = datetime.datetime.now()

        instance = cls(
            guid=video.guid or '',
            name=video.title or '',
            description=video.description or '',
            website_url=video.link or '',
            when_published=video.publish_datetime,
            file_url=video.file_url or '',
            file_url_mimetype=video.file_url_mimetype or '',
            file_url_length=video.file_url_length,
            when_submitted=now,
            when_approved=now if status == cls.ACTIVE else None,
            status=status,
            thumbnail_url=video.thumbnail_url or '',
            embed_code=video.embed_code or '',
            flash_enclosure_url=video.flash_enclosure_url or '',
            video_service_user=video.user or '',
            video_service_url=video.user_url or '',
            **kwargs
        )

        if instance.description:
            soup = BeautifulSoup(video.description)
            for tag in soup.findAll(
                'div', {'class': "miro-community-description"}):
                instance.description = tag.renderContents()
                break
            instance.description = sanitize(instance.description,
                                            extra_filters=['img'])

        instance._vidscraper_video = video

        if source_import is not None:
            source_import.set_video_source(instance)

        def save_m2m():
            if authors:
                instance.authors = authors
            if categories:
                instance.categories = categories
            if video.tags:
                tags = set(tag.strip() for tag in video.tags if tag.strip())
                for tag_name in tags:
                    if settings.FORCE_LOWERCASE_TAGS:
                        tag_name = tag_name.lower()
                    tag, created = \
                        tagging.models.Tag._default_manager.db_manager(
                        using).get_or_create(name=tag_name)
                    tagging.models.TaggedItem._default_manager.db_manager(
                        using).create(
                        tag=tag, object=instance)
            if source_import is not None:
                source_import.handle_video(instance, video, using)
            post_video_from_vidscraper.send(sender=cls, instance=instance,
                                            vidscraper_video=video, using=using)

        if commit:
            instance.save(using=using)
            save_m2m()
        else:
            instance._state.db = using
            instance.save_m2m = save_m2m
        return instance

    def get_tags(self):
        if self.pk is None:
            vidscraper_video = getattr(self, '_vidscraper_video', None)
            return getattr(vidscraper_video, 'tags', None) or []
        return self.tags

    def try_to_get_file_url_data(self):
        """
        Do a HEAD request on self.file_url to find information about
        self.file_url_length and self.file_url_mimetype

        Note that while this method fills in those attributes, it does *NOT*
        run self.save() ... so be sure to do so after calling this method!
        """
        if not self.file_url:
            return

        request = urllib2.Request(quote_unicode_url(self.file_url))
        request.get_method = lambda: 'HEAD'
        try:
            http_file = urllib2.urlopen(request)
        except Exception:
            pass
        else:
            self.file_url_length = http_file.headers.get('content-length')
            self.file_url_mimetype = http_file.headers.get('content-type', '')
            if self.file_url_mimetype in ('application/octet-stream', ''):
                # We got a not-useful MIME type; guess!
                guess = mimetypes.guess_type(self.file_url)
                if guess[0] is not None:
                    self.file_url_mimetype = guess[0]

    def save_thumbnail(self):
        """
        Automatically run the entire file saving process... provided we have a
        thumbnail_url, that is.
        """
        if not self.thumbnail_url:
            return

        try:
            content_thumb = ContentFile(urllib.urlopen(
                    quote_unicode_url(self.thumbnail_url)).read())
        except IOError:
            raise CannotOpenImageUrl('IOError loading %s' % self.thumbnail_url)
        except httplib.InvalidURL:
            # if the URL isn't valid, erase it and move on
            self.thumbnail_url = ''
            self.has_thumbnail = False
            self.save()
        else:
            try:
                self.save_thumbnail_from_file(content_thumb)
            except Exception:
                logging.exception("Error while getting " + repr(self.thumbnail_url))

    def submitter(self):
        """
        Return the user that submitted this video.  If necessary, use the
        submitter from the originating feed or savedsearch.
        """
        if self.user is not None:
            return self.user
        elif self.feed is not None:
            return self.feed.user
        elif self.search is not None:
            return self.search.user
        else:
            # XXX warning?
            return None

    def when(self):
        """
        Simple method for getting the when_published date if the video came
        from a feed or a search, otherwise the when_approved date.
        """
        if SiteLocation.objects.using(self._state.db).get(
            site=self.site_id).use_original_date and \
            self.when_published:
            return self.when_published
        return self.when_approved or self.when_submitted

    def source_type(self):
        return video__source_type(self)

    def video_service(self):
        return video__video_service(self)

    def when_prefix(self):
        """
        When videos are bulk imported (from a feed or a search), we list the
        date as "published", otherwise we show 'posted'.
        """

        if self.when_published and \
                SiteLocation.objects.get(site=self.site_id).use_original_date:
            return 'published'
        else:
            return 'posted'

    def voting_enabled(self):
        if not voting_enabled():
            return False
        return self.categories.filter(contest_mode__isnull=False).exists()

def video__source_type(self):
    '''This is not a method of the Video so that we can can call it from South.'''

    try:
        if self.id and self.search:
            return u'Search: %s' % self.search
        elif self.id and self.feed_id is not None:
            if self.feed.video_service():
                return u'User: %s: %s' % (
                    self.feed.video_service(),
                    self.feed.name)
            else:
                return 'Feed: %s' % self.feed.name
        elif self.video_service_user:
            return u'User: %s: %s' % (
                video__video_service(self),
                self.video_service_user)
        else:
            return ''
    except ObjectDoesNotExist:
        return ''

def pre_save_video_set_calculated_source_type(instance, **kwargs):
    # Always recalculate the source_type field.
    instance.calculated_source_type = video__source_type(instance)
    return instance
models.signals.pre_save.connect(pre_save_video_set_calculated_source_type,
                                sender=Video)

def video__video_service(self):
    '''This is not a method of Video so we can call it from a South migration.'''
    if not self.website_url:
        return

    url = self.website_url
    for service, regexp in VIDEO_SERVICE_REGEXES:
        if re.search(regexp, url, re.I):
            return service

class Watch(models.Model):
    """
    Record of a video being watched.

    fields:
     - video: Video that was watched
     - timestamp: when watched
     - user: user that watched it, if any
     - ip_address: IP address of the user
    """
    video = models.ForeignKey(Video)
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey('auth.User', blank=True, null=True)
    ip_address = models.IPAddressField()

    class Meta:
        app_label = 'localtv'

    @classmethod
    def add(Class, request, video):
        """
        Adds a record of a watched video to the database.  If the request came
        from localhost, check to see if it was forwarded to (hopefully) get the
        right IP address.
        """
        ip = request.META.get('REMOTE_ADDR', '0.0.0.0')
        if not ipv4_re.match(ip):
            ip = '0.0.0.0'

        if hasattr(request, 'user') and request.user.is_authenticated():
            user = request.user
        else:
            user = None

        try:
            Class(video=video, user=user, ip_address=ip).save()
        except Exception:
            pass


class VideoModerator(CommentModerator):

    def allow(self, comment, video, request):
        sitelocation = SiteLocation.objects.get(site=video.site)
        if sitelocation.comments_required_login:
            return request.user and request.user.is_authenticated()
        else:
            return True

    def email(self, comment, video, request):
        # we do the import in the function because otherwise there's a circular
        # dependency
        from localtv.utils import send_notice

        sitelocation = SiteLocation.objects.get(site=video.site)
        t = loader.get_template('comments/comment_notification_email.txt')
        c = Context({ 'comment': comment,
                      'content_object': video,
                      'user_is_admin': True})
        subject = '[%s] New comment posted on "%s"' % (video.site.name,
                                                       video)
        message = t.render(c)
        send_notice('admin_new_comment', subject, message,
                    sitelocation=sitelocation)

        admin_new_comment = notification.NoticeType.objects.get(
            label="admin_new_comment")

        if video.user and video.user.email:
            video_comment = notification.NoticeType.objects.get(
                label="video_comment")
            if notification.should_send(video.user, video_comment, "1") and \
               not notification.should_send(video.user,
                                            admin_new_comment, "1"):
               c = Context({ 'comment': comment,
                             'content_object': video,
                             'user_is_admin': False})
               message = t.render(c)
               EmailMessage(subject, message, settings.DEFAULT_FROM_EMAIL,
                            [video.user.email]).send(fail_silently=True)

        comment_post_comment = notification.NoticeType.objects.get(
            label="comment_post_comment")
        previous_users = set()
        for previous_comment in comment.__class__.objects.filter(
            content_type=comment.content_type,
            object_pk=video.pk,
            is_public=True,
            is_removed=False,
            submit_date__lte=comment.submit_date,
            user__email__isnull=False).exclude(
            user__email='').exclude(pk=comment.pk):
            if (previous_comment.user not in previous_users and
                notification.should_send(previous_comment.user,
                                         comment_post_comment, "1") and
                not notification.should_send(previous_comment.user,
                                             admin_new_comment, "1")):
                previous_users.add(previous_comment.user)
                c = Context({ 'comment': comment,
                              'content_object': video,
                              'user_is_admin': False})
                message = t.render(c)
                EmailMessage(subject, message, settings.DEFAULT_FROM_EMAIL,
                             [previous_comment.user.email]).send(fail_silently=True)

    def moderate(self, comment, video, request):
        sitelocation = SiteLocation.objects.get(site=video.site)
        if sitelocation.screen_all_comments:
            if not getattr(request, 'user'):
                return True
            else:
                return not sitelocation.user_is_admin(request.user)
        else:
            return False

moderator.register(Video, VideoModerator)

tagging.register(Video)
tagging.register(OriginalVideo)

def send_new_video_email(sender, **kwargs):
    sitelocation = SiteLocation.objects.get(site=sender.site)
    if sender.is_active():
        # don't send the e-mail for videos that are already active
        return
    t = loader.get_template('localtv/submit_video/new_video_email.txt')
    c = Context({'video': sender})
    message = t.render(c)
    subject = '[%s] New Video in Review Queue: %s' % (sender.site.name,
                                                          sender)
    send_notice('admin_new_submission',
                     subject, message,
                     sitelocation=sitelocation)

submit_finished.connect(send_new_video_email, weak=False)

def delete_comments(sender, instance, **kwargs):
    from django.contrib.comments import get_model
    get_model().objects.filter(object_pk=instance.pk,
                               content_type__app_label='localtv',
                               content_type__model='video'
                               ).delete()
models.signals.pre_delete.connect(delete_comments,
                                  sender=Video)

def create_original_video(sender, instance=None, created=False, **kwargs):
    if not created:
        return # don't care about saving
    if not instance.website_url:
        # we don't know how to scrape this, so ignore it
        return
    new_data = dict(
        (field.name, getattr(instance, field.name))
        for field in VideoBase._meta.fields)
    OriginalVideo.objects.db_manager(instance._state.db).create(
        video=instance,
        thumbnail_updated=datetime.datetime.now(),
        **new_data)

def save_original_tags(sender, instance, created=False, **kwargs):
    if not created:
        # not a new tagged item
        return
    if not isinstance(instance.object, Video):
        # not a video
        return
    if (instance.object.when_submitted - datetime.datetime.now() >
        datetime.timedelta(seconds=10)):
        return
    try:
        original = instance.object.original
    except OriginalVideo.DoesNotExist:
        return
    tagging.models.TaggedItem.objects.db_manager(instance._state.db).create(
        tag=instance.tag, object=original)

if ENABLE_ORIGINAL_VIDEO:
    models.signals.post_save.connect(create_original_video,
                                     sender=Video)
    models.signals.post_save.connect(save_original_tags,
                                     sender=tagging.models.TaggedItem)
