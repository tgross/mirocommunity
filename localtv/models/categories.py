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

from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.db import models
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _

from localtv.models.videos import Video
from localtv.settings import voting_enabled


class Category(models.Model):
    """
    A category for videos to be contained in.

    Categories and tags aren't too different functionally, but categories are
    more strict as they can't be defined by visitors.  Categories can also be
    hierarchical.

    Fields:
     - site: A link to the django.contrib.sites.models.Site object this object
       is bound to
     - name: Name of this category
     - slug: a slugified verison of the name, used to create more friendly URLs
     - logo: An image to associate with this category
     - description: human readable description of this item
     - parent: Reference to another Category.  Allows you to have heirarchical
       categories.
    """
    site = models.ForeignKey(Site)
    name = models.CharField(
        max_length=80, verbose_name='Category Name',
        help_text=_("The name is used to identify the category almost "
                    "everywhere; for example, under a video or in a "
                    "category widget."))
    slug = models.SlugField(
        verbose_name='Category Slug',
        help_text=_("The \"slug\" is the URL-friendly version of the name.  It "
                    "is usually lower-case and contains only letters, numbers "
                    "and hyphens."))
    logo = models.ImageField(
        upload_to="localtv/category_logos", blank=True,
        verbose_name='Thumbnail/Logo',
        help_text=_("Optional. For example: a leaf for 'environment' or the "
                    "logo of a university department."))
    description = models.TextField(
        blank=True, verbose_name='Description (HTML)',
        help_text=_("Optional. The description is not prominent by default, but"
                    " some themes may show it."))
    parent = models.ForeignKey(
        'self', blank=True, null=True,
        related_name='child_set',
        verbose_name='Category Parent',
        help_text=_("Categories, unlike tags, can have a hierarchy."))

    # only relevant is voting is enabled for the site
    contest_mode = models.DateTimeField('Turn on Contest',
                                        null=True,
                                        default=None)

    class Meta:
        ordering = ['name']
        unique_together = (
            ('slug', 'site'),
            ('name', 'site'))
        app_label = 'localtv'

    def __unicode__(self):
        return self.name

    def depth(self):
        """
        Returns the number of parents this category has.  Used for indentation.
        """
        depth = 0
        parent = self.parent
        while parent is not None:
            depth += 1
            parent = parent.parent
        return depth

    def dashes(self):
        return mark_safe('&mdash;' * self.depth())

    @models.permalink
    def get_absolute_url(self):
        return ('localtv_category', [self.slug])

    @classmethod
    def in_order(klass, sitelocation, initial=None):
        objects = []
        def accumulate(categories):
            for category in categories:
                objects.append(category)
                if category.child_set.count():
                    accumulate(category.child_set.all())
        if initial is None:
            initial = klass.objects.filter(site=sitelocation, parent=None)
        accumulate(initial)
        return objects

    def approved_set(self):
        """
        Returns active videos for the category and its subcategories, ordered
        by decreasing best date.
        
        """
        categories = [self] + self.in_order(self.site, self.child_set.all())
        return Video.objects.active().filter(
            categories__in=categories).distinct()
    approved_set = property(approved_set)

    def unique_error_message(self, model_class, unique_check):
        return 'Category with this %s already exists.' % (
            unique_check[0],)

    def has_votes(self):
        """
        Returns True if this category has videos with votes.
        """
        if not voting_enabled():
            return False
        import voting
        return voting.models.Vote.objects.filter(
            content_type=ContentType.objects.get_for_model(Video),
            object_id__in=self.approved_set.values_list('id',
                                                        flat=True)).exists()
