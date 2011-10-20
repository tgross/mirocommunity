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

from django.conf.urls.defaults import patterns, include, url
from django.contrib.auth.models import User
from django.views.generic import ListView

from localtv.listing.views import VideoSearchView, SiteListView
from localtv.models import Category

# "Base" patterns
urlpatterns = patterns(
    'localtv.views',
    url(r'^$', 'index', name='localtv_index'),
    url(r'^about/$', 'about', name='localtv_about'),
    url(r'^share/(\d+)/(\d+)', 'share_email', name='email-share'),
    url(r'^video/(?P<video_id>[0-9]+)/(?P<slug>[\w-]*)/?$', 'view_video',
                    name='localtv_view_video'),
    url(r'^newsletter/$', 'newsletter', name='localtv_newsletter'))

# Listing patterns
# This has to be importable for now because of a hack in the view_video view
# which imports this view to check whether the referer was a category page.
category_videos = VideoSearchView.as_view(
    template_name='localtv/category.html',
    default_filter='category'
)
urlpatterns += patterns(
    'localtv.listing.views',
    url(r'^search/$', VideoSearchView.as_view(
                        template_name='localtv/video_listing_search.html',
                    ), name='localtv_search'),
    url(r'^category/$', SiteListView.as_view(
                        template_name='localtv/categories.html',
                        queryset=Category.objects.filter(parent=None),
                        paginate_by=15
                    ), name='localtv_category_index'),
    url(r'^category/(?P<slug>[-\w]+)/$', category_videos,
                    name='localtv_category'),
    url(r'^author/$', ListView.as_view(
                        template_name='localtv/author_list.html',
                        model=User,
                        context_object_name='authors'
                    ), name='localtv_author_index'),
    url(r'^author/(?P<pk>\d+)/$', VideoSearchView.as_view(
                        template_name='localtv/author.html',
                        default_filter='author',
                        default_sort='-date'
                    ), name='localtv_author'))

# Comments patterns
urlpatterns += patterns(
    'localtv.comments.views',
    url(r'^comments/post/$', 'post_comment', name='comments-post-comment'),
    url(r'^comments/moderation-queue$', 'moderation_queue', {},
                    'comments-moderation-queue'),
    url(r'^comments/moderation-queue/undo$', 'undo', {},
                    'comments-moderation-undo'),
    url(r'^comments/', include('django.contrib.comments.urls')))

# Various inclusions
urlpatterns += patterns(
    '',
    url(r'^accounts/logout/$', 'django.contrib.auth.views.logout', {
                    'next_page': '/'}),
    url(r'^accounts/profile/', include('localtv.user_profile.urls')),
    url(r'^accounts/', include('socialauth.urls')),
    url(r'^accounts/', include('registration.backends.default.urls')),
    url(r'^admin/edit_attributes/', include('localtv.inline_edit.urls')),
    url(r'^admin/', include('localtv.admin.urls')),
    url(r'^submit_video/', include('localtv.submit_video.urls')),
    url(r'^listing/', include('localtv.listing.urls')),
    url(r'^feeds/', include('localtv.feeds.urls')),
    url(r'^goodies/', include('localtv.goodies.urls')),
    url(r'^share/', include('email_share.urls')),
    url(r'^playlists/', include('localtv.playlists.urls')))
