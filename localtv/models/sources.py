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
import re
import logging
import sys
import traceback

from django.conf import settings
from django.contrib.sites.models import Site
from django.db import models
from django.utils.translation import ugettext_lazy as _
import vidscraper

from localtv.models.base import (THUMB_SIZES, VIDEO_SERVICE_REGEXES,
                                 Thumbnailable, StatusedThumbnailable)
from localtv.models.videos import Video


class Source(Thumbnailable):
    """
    An abstract base class to represent things which are sources of multiple
    videos.  Current subclasses are Feed and SavedSearch.
    """
    id = models.AutoField(primary_key=True)
    site = models.ForeignKey(Site)
    auto_approve = models.BooleanField(default=False)
    auto_update = models.BooleanField(default=True,
                                      help_text=_("If selected, new videos will"
                                                  " automatically be imported "
                                                  "from this source."))
    user = models.ForeignKey('auth.User', null=True, blank=True)
    auto_categories = models.ManyToManyField("localtv.Category", blank=True)
    auto_authors = models.ManyToManyField("auth.User", blank=True,
                                          related_name='auto_%(class)s_set')

    THUMB_SIZES = THUMB_SIZES

    class Meta:
        abstract = True

    def update(self, video_iter, source_import, using='default',
               clear_rejected=True):
        """
        Imports videos from a feed/search.  `videos` is an iterable which
        returns :class:`vidscraper.suites.base.Video` objects.  We use
        :method:`.Video.from_vidscraper_video` to map the Vidscraper fields to
        Video attributes.

        If ``clear_rejected`` is ``True``, rejected versions of videos that are
        found in the ``video_iter`` will be deleted and re-imported.

        """
        author_pks = list(self.auto_authors.values_list('pk', flat=True))
        category_pks = list(self.auto_categories.values_list('pk', flat=True))

        import_opts = source_import.__class__._meta

        from localtv.tasks import video_from_vidscraper_video, mark_import_complete

        total_videos = 0

        for vidscraper_video in video_iter:
            total_videos += 1
            
            video_from_vidscraper_video.delay(
                vidscraper_video,
                site_pk=self.site_id,
                import_app_label=import_opts.app_label,
                import_model=import_opts.module_name,
                import_pk=source_import.pk,
                status=Video.UNAPPROVED,
                author_pks=author_pks,
                category_pks=category_pks,
                clear_rejected=clear_rejected,
                using=using)

        source_import.__class__._default_manager.using(using).filter(
            pk=source_import.pk
        ).update(
            total_videos=total_videos
        )
        mark_import_complete.delay(import_app_label=import_opts.app_label,
                                   import_model=import_opts.module_name,
                                   import_pk=source_import.pk,
                                   using=using)


class Feed(Source, StatusedThumbnailable):
    """
    Feed to pull videos in from.

    If the same feed is used on two different , they will require two
    separate entries here.

    Fields:
      - feed_url: The location of this field
      - site: which site this feed belongs to
      - name: human readable name for this feed
      - webpage: webpage that this feed\'s content is associated with
      - description: human readable description of this item
      - last_updated: last time we ran self.update_items()
      - when_submitted: when this feed was first registered on this site
      - status: one of Feed.STATUS_CHOICES
      - etag: used to see whether or not the feed has changed since our last
        update.
      - auto_approve: whether or not to set all videos in this feed to approved
        during the import process
      - user: a user that submitted this feed, if any
      - auto_categories: categories that are automatically applied to videos on
        import
      - auto_authors: authors that are automatically applied to videos on
        import
    """
    feed_url = models.URLField(verify_exists=False)
    name = models.CharField(max_length=250)
    webpage = models.URLField(verify_exists=False, blank=True)
    description = models.TextField()
    last_updated = models.DateTimeField()
    when_submitted = models.DateTimeField(auto_now_add=True)
    etag = models.CharField(max_length=250, blank=True)
    avoid_frontpage = models.BooleanField(default=False)
    calculated_source_type = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        unique_together = (
            ('feed_url', 'site'))
        get_latest_by = 'last_updated'
        app_label = 'localtv'

    def __unicode__(self):
        return self.name

    @models.permalink
    def get_absolute_url(self):
        return ('localtv_list_feed', [self.pk])

    def update(self, using='default', **kwargs):
        """
        Fetch and import new videos from this feed.

        """
        try:
            FeedImport.objects.using(using).get(source=self,
                                                status=FeedImport.STARTED)
        except FeedImport.DoesNotExist:
            pass
        else:
            logging.debug('Skipping import of %s: already in progress' % self)
            return

        feed_import = FeedImport.objects.db_manager(using).create(source=self,
                                                auto_approve=self.auto_approve)

        video_iter = vidscraper.auto_feed(
            self.feed_url,
            crawl=(getattr(self, 'status', True) == 0),
            api_keys={
                'vimeo_key': getattr(settings, 'VIMEO_API_KEY', None),
                'vimeo_secret': getattr(settings, 'VIMEO_API_SECRET', None),
                'ustream_key': getattr(settings, 'USTREAM_API_KEY', None)
            }
        )

        try:
            video_iter.load()
        except Exception:
            feed_import.last_activity = datetime.datetime.now()
            feed_import.status = FeedImport.FAILED
            feed_import.save()
            feed_import.handle_error(u'Skipping import of %s: error loading the'
                                     u' feed' % self,
                                     with_exception=True, using=using)
            return

        super(Feed, self).update(video_iter, source_import=feed_import,
                                 using=using, **kwargs)

        self.etag = getattr(video_iter, 'etag', None) or ''
        self.last_updated = (getattr(video_iter, 'last_modified', None) or
                                 datetime.datetime.now())
        self.save()

    def source_type(self):
        return self.calculated_source_type

    def _calculate_source_type(self):
        return _feed__calculate_source_type(self)

    def video_service(self):
        return feed__video_service(self)

def feed__video_service(feed):
    # This implements the video_service method. It's outside the Feed class
    # so we can use it safely from South.
    for service, regexp in VIDEO_SERVICE_REGEXES:
        if re.search(regexp, feed.feed_url, re.I):
            return service

def _feed__calculate_source_type(feed):
    # This implements the _calculate_source_type method. It's outside the Feed
    # class so we can use it safely from South.
    video_service = feed__video_service(feed)
    if video_service is None:
        return u'Feed'
    else:
        return u'User: %s' % video_service

def pre_save_set_calculated_source_type(instance, **kwargs):
    # Always save the calculated_source_type
    instance.calculated_source_type = _feed__calculate_source_type(instance)
    # Plus, if the name changed, we have to recalculate all the Videos that depend on us.
    try:
        v = Feed.objects.using(instance._state.db).get(id=instance.id)
    except Feed.DoesNotExist:
        return instance
    if v.name != instance.name:
        # recalculate all the sad little videos' calculated_source_type
        for vid in instance.video_set.all():
            vid.save()
    return instance
models.signals.pre_save.connect(pre_save_set_calculated_source_type,
                                sender=Feed)


class SavedSearch(Source):
    """
    A set of keywords to regularly pull in new videos from.

    There's an administrative interface for doing "live searches"

    Fields:
     - site: site this savedsearch applies to
     - query_string: a whitespace-separated list of words to search for.  Words
       starting with a dash will be processed as negative query terms
     - when_created: date and time that this search was saved.
    """
    query_string = models.TextField()
    when_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'localtv'

    def __unicode__(self):
        return self.query_string

    def update(self, using='default', **kwargs):
        """
        Fetch and import new videos from this search.

        """
        try:
            SearchImport.objects.using(using).get(source=self,
                                                  status=SearchImport.STARTED)
        except SearchImport.DoesNotExist:
            pass
        else:
            logging.debug('Skipping import of %s: already in progress' % self)
            return

        search_import = SearchImport.objects.db_manager(using).create(
            source=self,
            auto_approve=self.auto_approve
        )

        searches = vidscraper.auto_search(
            self.query_string,
            crawl=True,
            api_keys={
                'vimeo_key': getattr(settings, 'VIMEO_API_KEY', None),
                'vimeo_secret': getattr(settings, 'VIMEO_API_SECRET', None),
                'ustream_key': getattr(settings, 'USTREAM_API_KEY', None)
            }
        )

        # Mark the import as "ended" immediately if none of the searches can
        # load.
        should_end = True
        for video_iter in searches.values():
            try:
                video_iter.load()
            except Exception:
                search_import.handle_error(u'Skipping import of search results '
                               u'from %s' % video_iter.suite.__class__.__name__,
                               with_exception=True, using=using)
                continue
            should_end = False
            super(SavedSearch, self).update(video_iter,
                                            source_import=search_import,
                                            using=using, **kwargs)
        if should_end:
            search_import.status = SearchImport.FAILED
            search_import.last_activity = datetime.datetime.now()
            search_import.save()
            logging.debug('All searches failed for %s' % self)

    def source_type(self):
        return u'Search'


class SourceImportIndex(models.Model):
    video = models.OneToOneField('Video', unique=True)
    index = models.PositiveIntegerField(blank=True, null=True)
    
    class Meta:
        abstract = True


class FeedImportIndex(SourceImportIndex):
    source_import = models.ForeignKey('FeedImport', related_name='indexes')

    class Meta:
        app_label = 'localtv'


class SearchImportIndex(SourceImportIndex):
    source_import = models.ForeignKey('SearchImport', related_name='indexes')
    #: This is just the name of the suite that was used to get this index.
    suite = models.CharField(max_length=30)

    class Meta:
        app_label = 'localtv'


class SourceImportError(models.Model):
    message = models.TextField()
    traceback = models.TextField(blank=True)
    is_skip = models.BooleanField(help_text="Whether this error represents a "
                                            "video that was skipped.")
    datetime = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class FeedImportError(SourceImportError):
    source_import = models.ForeignKey('FeedImport', related_name='errors')

    class Meta:
        app_label = 'localtv'


class SearchImportError(SourceImportError):
    source_import = models.ForeignKey('SearchImport', related_name='errors')

    class Meta:
        app_label = 'localtv'


class SourceImport(models.Model):
    STARTED = 'started'
    COMPLETE = 'complete'
    FAILED = 'failed'
    STATUS_CHOICES = (
        (STARTED, _('Started')),
        (COMPLETE, _('Complete')),
        (FAILED, _('Failed'))
    )
    start = models.DateTimeField(auto_now_add=True)
    last_activity = models.DateTimeField(blank=True, null=True)
    total_videos = models.PositiveIntegerField(blank=True, null=True)
    videos_imported = models.PositiveIntegerField(default=0)
    videos_skipped = models.PositiveIntegerField(default=0)
    #: Caches the auto_approve of the search on the import, so that the imported
    #: videos can be approved en masse at the end of the import based on the
    #: settings at the beginning of the import.
    auto_approve = models.BooleanField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES,
                              default=STARTED)

    class Meta:
        get_latest_by = 'start'
        ordering = ['-start']
        abstract = True

    def set_video_source(self, video):
        """
        Sets the value of the correct field on the ``video`` to mark it as
        having the same source as this import. Must be implemented by
        subclasses.

        """
        raise NotImplementedError

    def get_videos(self, using='default'):
        raise NotImplementedError

    def handle_error(self, message, is_skip=False, with_exception=False,
                     using='default'):
        """
        Logs the error with the default logger and to the database.

        :param message: A human-friendly description of the error that does
                        not contain sensitive information.
        :param is_skip: ``True`` if the error results in a video being skipped.
                        Default: False.
        :param with_exception: ``True`` if exception information should be
                               recorded. Default: False.
        :param using: The database to use. Default: 'default'.

        """
        if with_exception:
            logging.debug(message, with_exception=True)
            tb = ''.join(traceback.format_exception(*sys.exc_info()))
        else:
            logging.debug(message)
            tb = ''
        self.errors.db_manager(using).create(message=message,
                                             source_import=self,
                                             traceback=tb,
                                             is_skip=is_skip)
        if is_skip:
            self.__class__._default_manager.using(using).filter(pk=self.pk
                        ).update(videos_skipped=models.F('videos_skipped') + 1)
            from localtv.tasks import mark_import_complete
            mark_import_complete.delay(import_app_label=self._meta.app_label,
                                       import_model=self._meta.module_name,
                                       import_pk=self.pk,
                                       using=using)

    def get_index_creation_kwargs(self, video, vidscraper_video):
        return {
            'source_import': self,
            'video': video,
            'index': vidscraper_video.index
        }

    def handle_video(self, video, vidscraper_video, using='default'):
        """
        Creates an index instance connecting the video to this import.

        :param video: The :class:`Video` instance which was imported.
        :param vidscraper_video: The original video from :mod:`vidscraper`.
        :param using: The database alias to use. Default: 'default'

        """
        self.indexes.db_manager(using).create(
                    **self.get_index_creation_kwargs(video, vidscraper_video))
        self.__class__._default_manager.using(using).filter(pk=self.pk
                    ).update(videos_imported=models.F('videos_imported') + 1)
        from localtv.tasks import mark_import_complete
        mark_import_complete.delay(import_app_label=self._meta.app_label,
                                   import_model=self._meta.module_name,
                                   import_pk=self.pk,
                                   using=using)


class FeedImport(SourceImport):
    source = models.ForeignKey(Feed, related_name='imports')

    class Meta:
        app_label = 'localtv'

    def set_video_source(self, video):
        video.feed_id = self.source_id

    def get_videos(self, using='default'):
        return Video.objects.using(using).filter(
                                        feedimportindex__source_import=self)


class SearchImport(SourceImport):
    source = models.ForeignKey(SavedSearch, related_name='imports')

    class Meta:
        app_label = 'localtv'

    def set_video_source(self, video):
        video.search_id = self.source_id

    def get_videos(self, using='default'):
        return Video.objects.using(using).filter(
                                        searchimportindex__source_import=self)

    def get_index_creation_kwargs(self, video, vidscraper_video):
        kwargs = super(SearchImport, self).get_index_creation_kwargs(video,
                                                            vidscraper_video)
        kwargs['suite'] = vidscraper_video.suite.__class__.__name__
        return kwargs
