# This file is part of Miro Community.
# Copyright (C) 2010 Participatory Culture Foundation
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

from django.conf import settings
from django.db.models import Count, signals
from django.forms.models import model_to_dict
from django.utils.encoding import force_unicode
from haystack import indexes
from haystack import site

from localtv.models import Video, Watch
from localtv.search.utils import SortFilterMixin
from localtv.tasks import haystack_update_index


class QueuedSearchIndex(indexes.SearchIndex):
    def _setup_save(self, model):
        signals.post_save.connect(self._enqueue_update, sender=model)

    def _setup_delete(self, model):
        signals.post_delete.connect(self._enqueue_removal, sender=model)

    def _teardown_save(self, model):
        signals.post_save.disconnect(self._enqueue_update, sender=model)

    def _teardown_delete(self, model):
        signals.post_delete.connect(self._enqueue_removal, sender=model)

    def _enqueue_update(self, instance, **kwargs):
        self._enqueue_instance(instance, False)

    def _enqueue_removal(self, instance, **kwargs):
        self._enqueue_instance(instance, True)

    def _enqueue_instance(self, instance, is_removal):
        haystack_update_index.delay(instance._meta.app_label,
                                    instance._meta.module_name,
                                    instance.pk,
                                    is_removal,
                                    settings=settings.SETTINGS_MODULE)


class VideoIndex(QueuedSearchIndex):
    text = indexes.CharField(document=True, use_template=True)

    # HACK because xapian-haystack django_id/pk filtering is broken.
    pk_hack = indexes.IntegerField(model_attr='pk')

    # ForeignKey relationships
    feed = indexes.IntegerField(model_attr='feed_id', null=True)
    search = indexes.IntegerField(model_attr='search_id', null=True)
    user = indexes.IntegerField(model_attr='user_id', null=True)
    site = indexes.IntegerField(model_attr='site_id')

    # M2M relationships
    tags = indexes.MultiValueField()
    categories = indexes.MultiValueField()
    authors = indexes.MultiValueField()
    playlists = indexes.MultiValueField()

    # Aggregated/collated data.
    best_date = indexes.DateTimeField(model_attr='when')
    watch_count = indexes.IntegerField()
    last_featured = indexes.DateTimeField(model_attr='last_featured',
                            default=SortFilterMixin._empty_value['featured'])
    when_approved = indexes.DateTimeField(model_attr='when_approved',
                            default=SortFilterMixin._empty_value['approved'])

    def _setup_save(self, model):
        super(VideoIndex, self)._setup_save(model)
        signals.post_save.connect(self._enqueue_watch_update,
                                  sender=Watch)

    def _teardown_save(self, model):
        super(VideoIndex, self)._teardown_save(model)
        signals.post_save.disconnect(self._enqueue_watch_update,
                                     sender=Watch)

    def _enqueue_watch_update(self, instance, **kwargs):
        self._enqueue_instance(instance.video, False)

    def index_queryset(self):
        """
        Custom queryset to only search active videos and to annotate them
        with the watch_count.

        """
        return self.model._default_manager.filter(status=self.model.ACTIVE
                                         ).annotate(watch_count=Count('watch'))

    def read_queryset(self):
        """
        Adds a select_related call to the normal :meth:`.index_queryset`; the
        related items only need to be in the index by id, but on read we will
        probably need more.

        """
        return self.index_queryset().select_related('feed', 'user', 'search')

    def get_updated_field(self):
        return 'when_modified'

    def _prepare_field(self, video, field):
        return [int(rel.pk) for rel in getattr(video, field).all()]

    def prepare_tags(self, video):
        return self._prepare_field(video, 'tags')

    def prepare_categories(self, video):
        return self._prepare_field(video, 'categories')

    def prepare_authors(self, video):
        return self._prepare_field(video, 'authors')

    def prepare_playlists(self, video):
        return self._prepare_field(video, 'playlists')

    def prepare_watch_count(self, video):
        # video.watch_count is set during :meth:`~VideoIndex.index_queryset`.
        # If for some reason that isn't available, do a manual count.
        try:
            return video.watch_count
        except AttributeError:
            return video.watch_set.count()

    def _enqueue_instance(self, instance, is_removal):
        if (not instance.name and not instance.description
            and not instance.website_url and not instance.file_url):
            # fake instance for testing. TODO: This should probably not be done.
            return
        super(VideoIndex, self)._enqueue_instance(instance, is_removal)

site.register(Video, VideoIndex)
