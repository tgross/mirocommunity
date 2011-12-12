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

try:
    from PIL import Image
except ImportError:
    import Image

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import models
from django.utils.translation import ugettext_lazy as _
from notification import models as notification
from tagging.models import Tag

from localtv.exceptions import CannotOpenImageUrl
from localtv.utils import resize_image_returning_list_of_strings


def delete_if_exists(path):
    if default_storage.exists(path):
        default_storage.delete(path)

UNAPPROVED_STATUS_TEXT = _(u'Unapproved')
ACTIVE_STATUS_TEXT = _(u'Active')
REJECTED_STATUS_TEXT = _(u'Rejected')
PENDING_THUMBNAIL_STATUS_TEXT = _(u'Waiting on thumbnail')
DISABLED_STATUS_TEXT = _(u'Disabled')

THUMB_SIZES = [ # for backwards, compatibility; it's now a class variable
    (534, 430), # behind a video
    (375, 295), # featured on frontpage
    (140, 110),
    (364, 271), # main thumb
    (222, 169), # medium thumb
    (88, 68),   # small thumb
]

 # Arguments for thumbnail resizing. Seem to be placed in the third position
 # of a THUMB_SIZES entry. TODO: Remove these constants with the thumbnail
 # refactor.
FORCE_HEIGHT_CROP = 1
# This second one is somehow related to facebook? But it's only used for
# SiteLocation thumbnails. Also loosely referenced in utils.py.
FORCE_HEIGHT_PADDING = 2

VIDEO_SERVICE_REGEXES = (
    ('YouTube', r'http://gdata\.youtube\.com/feeds/'),
    ('YouTube', r'http://(www\.)?youtube\.com/'),
    ('blip.tv', r'http://(.+\.)?blip\.tv/'),
    ('Vimeo', r'http://(www\.)?vimeo\.com/'),
    ('Dailymotion', r'http://(www\.)?dailymotion\.com/rss'))


class Thumbnailable(models.Model):
    """
    A type of Model that has thumbnails generated for it.
    """
    has_thumbnail = models.BooleanField(default=False)
    thumbnail_extension = models.CharField(max_length=8, blank=True)

    class Meta:
        abstract = True

    def save_thumbnail_from_file(self, content_thumb, resize=True):
        """
        Takes an image file-like object and stores it as the thumbnail for this
        video item.
        """
        try:
            pil_image = Image.open(content_thumb)
        except IOError:
            raise CannotOpenImageUrl('An image could not be loaded')

        # save an unresized version, overwriting if necessary
        delete_if_exists(
            self.get_original_thumb_storage_path())

        self.thumbnail_extension = pil_image.format.lower()
        default_storage.save(
            self.get_original_thumb_storage_path(),
            content_thumb)

        if hasattr(content_thumb, 'temporary_file_path'):
            # might have gotten moved by Django's storage system, so it might
            # be invalid now.  to make sure we've got a valid file, we reopen
            # under the new path
            content_thumb.close()
            content_thumb = default_storage.open(
                self.get_original_thumb_storage_path())
            pil_image = Image.open(content_thumb)

        if resize:
            # save any resized versions
            self.resize_thumbnail(pil_image)
        self.has_thumbnail = True
        self.save()

    def resize_thumbnail(self, thumb, resized_images=None):
        """
        Creates resized versions of the video's thumbnail image
        """
        if not thumb:
            thumb = Image.open(
                default_storage.open(self.get_original_thumb_storage_path()))
        if resized_images is None:
            resized_images = resize_image_returning_list_of_strings(
                thumb, self.THUMB_SIZES)
        for ( (width, height), data) in resized_images:
            # write file, deleting old thumb if it exists
            cf_image = ContentFile(data)
            delete_if_exists(
                self.get_resized_thumb_storage_path(width, height))
            default_storage.save(
                self.get_resized_thumb_storage_path(width, height),
                cf_image)

    def get_original_thumb_storage_path(self):
        """
        Return the path for the original thumbnail, relative to the default
        file storage system.
        """
        return 'localtv/%s_thumbs/%s/orig.%s' % (
            self._meta.object_name.lower(),
            self.id, self.thumbnail_extension)

    def get_resized_thumb_storage_path(self, width, height):
        """
        Return the path for the a thumbnail of a resized width and height,
        relative to the default file storage system.
        """
        return 'localtv/%s_thumbs/%s/%sx%s.png' % (
            self._meta.object_name.lower(),
            self.id, width, height)

    def delete_thumbnails(self):
        self.has_thumbnail = False
        delete_if_exists(self.get_original_thumb_storage_path())
        for size in self.THUMB_SIZES:
            delete_if_exists(
                self.get_resized_thumb_storage_path(*size[:2]))
        self.thumbnail_extension = ''
        self.save()

    def delete(self, *args, **kwargs):
        self.delete_thumbnails()
        super(Thumbnailable, self).delete(*args, **kwargs)


class StatusedThumbnailableQuerySet(models.query.QuerySet):

    def unapproved(self):
        return self.filter(status=StatusedThumbnailable.UNAPPROVED)

    def active(self):
        return self.filter(status=StatusedThumbnailable.ACTIVE)

    def rejected(self):
        return self.filter(status=StatusedThumbnailable.REJECTED)

    def pending_thumbnail(self):
        return self.filter(status=StatusedThumbnailable.PENDING_THUMBNAIL)


class StatusedThumbnailableManager(models.Manager):

    def get_query_set(self):
        return StatusedThumbnailableQuerySet(self.model, using=self._db)

    def unapproved(self):
        return self.get_query_set().unapproved()

    def active(self):
        return self.get_query_set().active()

    def rejected(self):
        return self.get_query_set().rejected()

    def pending_thumbnail(self):
        return self.get_query_set().pending_thumbnail()


class StatusedThumbnailable(models.Model):
    """
    Abstract class to provide the ``status`` field for Feeds and Videos.
    """
    #: An admin has not looked at this feed yet.
    UNAPPROVED = 0
    ACTIVE = 1
    #: This feed was rejected by an admin.
    REJECTED = 2
    PENDING_THUMBNAIL = 3

    STATUS_CHOICES = (
        (UNAPPROVED, UNAPPROVED_STATUS_TEXT),
        (ACTIVE, ACTIVE_STATUS_TEXT),
        (REJECTED, REJECTED_STATUS_TEXT),
        (PENDING_THUMBNAIL, PENDING_THUMBNAIL_STATUS_TEXT),
    )

    objects = StatusedThumbnailableManager()

    status = models.IntegerField(
        choices=STATUS_CHOICES, default=UNAPPROVED)

    def is_active(self):
        """Shortcut to check the common case of whether a video is active."""
        return self.status == self.ACTIVE

    class Meta:
        abstract = True

def tag_unicode(self):
    # hack to make sure that Unicode data gets returned for all tags
    if isinstance(self.name, str):
        self.name = self.name.decode('utf8')
    return self.name
Tag.__unicode__ = tag_unicode


def create_email_notices(app, created_models, verbosity, **kwargs):
    notification.create_notice_type('video_comment',
                                    'New comment on your video',
                                    'Someone commented on your video',
                                    default=2,
                                    verbosity=verbosity)
    notification.create_notice_type('comment_post_comment',
                                    'New comment after your comment',
                                    'Someone commented on a video after you',
                                    default=2,
                                    verbosity=verbosity)
    notification.create_notice_type('video_approved',
                                    'Your video was approved',
                                    'An admin approved your video',
                                    default=2,
                                    verbosity=verbosity)
    notification.create_notice_type('newsletter',
                                    'Newsletter',
                                    'Receive an occasional newsletter',
                                    default=2,
                                    verbosity=verbosity)
    notification.create_notice_type('admin_new_comment',
                                    'New comment',
                                    'A comment was submitted to the site',
                                    default=1,
                                    verbosity=verbosity)
    notification.create_notice_type('admin_new_submission',
                                    'New Submission',
                                    'A new video was submitted',
                                    default=1,
                                    verbosity=verbosity)
    notification.create_notice_type('admin_queue_weekly',
                                        'Weekly Queue Update',
                                    'A weekly e-mail of the queue status',
                                    default=1,
                                    verbosity=verbosity)
    notification.create_notice_type('admin_queue_daily',
                                    'Daily Queue Update',
                                    'A daily e-mail of the queue status',
                                    default=1,
                                    verbosity=verbosity)
    notification.create_notice_type('admin_video_updated',
                                    'Video Updated',
                                    'A video from a service was updated',
                                    default=1,
                                    verbosity=verbosity)
    notification.create_notice_type('admin_new_playlist',
                                    'Request for Playlist Moderation',
                                    'A new playlist asked to be public',
                                    default=2,
                                    verbosity=verbosity)

models.signals.post_syncdb.connect(create_email_notices)
