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

import logging
import os

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models.signals import post_save, post_delete

from localtv.models.sources import SavedSearch, Feed
from localtv.models.videos import Video
from localtv.utils import touch


### The "stamp" set of features is a performance optimization for large
### deployments of Miro Community.
###
### The VIDEO_PUBLISHED_STAMP updates the mtime of a file whenever a Video instance
### is created or modified. If the stamp file is really old, then you can
### safely skip running management commands like update_index.

ENABLE_CHANGE_STAMPS = getattr(
    settings, 'LOCALTV_ENABLE_CHANGE_STAMPS', False)


def update_stamp(name, override_date=None, delete_stamp=False):
    path = os.path.join(settings.MEDIA_ROOT, '.' + name)
    if delete_stamp:
        try:
            os.unlink(path)
        except OSError, e:
            if e.errno == 2: # does not exist
                pass
            else:
                raise
        return

    try:
        touch(path, override_date=override_date)
    except Exception, e:
        logging.error(e)


def video_published_stamp_signal_listener(sender=None, instance=None, created=False, override_date=None, **kwargs):
    '''The purpose of the change stamp is to create a file on-disk that
    indicates when a new instance of the Video model has been published
    or modified.

    We actually simply update the stamp on every change or deletion to
    Video instances. This is slightly too aggressive: If a Video comes in
    from a feed and is not published, we will update the stamp needlessly.

    That is okay with me for now.
    '''
    update_stamp(name='video-published-stamp', override_date=override_date)


def site_has_at_least_one_feed_stamp_signal_listener(sender=None, instance=None, created=False, override_date=None, **kwargs):
    '''The purpose of this stamp is to signify to management scripts that this
    site has at least one Feed.

    Therefore, it listens to all .save()s on the Feed model and makes sure
    that the site-has-at-least-one-feed-stamp file exists.

    The site-has-at-least-one-feed-stamp stamp is unique in that its modification time
    is not very important.
    '''
    update_stamp(name='site-has-at-least-one-feed-stamp', override_date=override_date)


def site_has_at_least_one_saved_search_stamp_signal_listener(sender=None, instance=None, created=False, override_date=None, **kwargs):
    '''The purpose of this stamp is to signify to management scripts that this
    site has at least one SavedSearch.

    It is mostly the same as site_has_at_least_one_feed_stamp_signal_listener.'''
    update_stamp(name='site-has-at-least-saved-search-stamp', override_date=override_date)


def user_modified_stamp_signal_listener(sender=None, instance=None, created=False, override_date=None, **kwargs):
    '''The purpose of this stamp is to listen to the User model, and whenever
    a User changes (perhaps due to a change in the last_login value), we create
    a file on-disk to say so.

    Note taht this is a little too aggressive: Any change to a User will cause this stamp
    to get updated, not just last_login-related changes.

    That is okay with me for now.
    '''
    update_stamp(name='user-modified-stamp', override_date=override_date)


def video_needs_published_date_stamp_signal_listener(instance=None, **kwargs):
    if instance.when_published is None:
        update_stamp(name='video-needs-published-date-stamp')


def create_or_delete_video_needs_published_date_stamp():
    '''This function takes a look at all the Videos. If there are any
    that have a NULL value for date_published, it updates the stamp.

    If not, it deletes the stamp.'''
    if Video.objects.filter(when_published__isnull=True):
        update_stamp(name='video-needs-published-date-stamp')
    else:
        update_stamp(name='video-needs-published-date-stamp', delete_stamp=True)


if ENABLE_CHANGE_STAMPS:
    post_save.connect(video_published_stamp_signal_listener, sender=Video)
    post_delete.connect(video_published_stamp_signal_listener, sender=Video)
    post_save.connect(user_modified_stamp_signal_listener, sender=User)
    post_delete.connect(user_modified_stamp_signal_listener, sender=User)
    post_save.connect(site_has_at_least_one_feed_stamp_signal_listener,
                      sender=Feed)
    post_save.connect(site_has_at_least_one_saved_search_stamp_signal_listener,
                      sender=SavedSearch)
    post_save.connect(video_needs_published_date_stamp_signal_listener,
                      sender=Video)
