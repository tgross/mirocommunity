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
import logging

from django.db import models
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.mail import EmailMessage
from django.core.signals import request_finished
from django.template.loader import render_to_string
from django.utils.html import escape
from django.utils.translation import ugettext_lazy as _
from notification import models as notification

from localtv import tiers
from localtv.models.base import (Thumbnailable, DISABLED_STATUS_TEXT,
                                 ACTIVE_STATUS_TEXT, FORCE_HEIGHT_PADDING)
from localtv.settings import DISABLE_TIERS_ENFORCEMENT


SITE_LOCATION_CACHE = {}


class SiteLocationManager(models.Manager):
    def get_current(self):
        sid = settings.SITE_ID
        try:
            # Dig it out of the cache.
            current_site_location = SITE_LOCATION_CACHE[sid]
        except KeyError:
            # Not in the cache? Time to put it in the cache.
            try:
                # If it is in the DB, get it.
                current_site_location = self.select_related().get(site__pk=sid)
            except SiteLocation.DoesNotExist:
                # Otherwise, create it.
                current_site_location = localtv.models.SiteLocation.objects.create(
                    site=Site.objects.get_current())

            SITE_LOCATION_CACHE[sid] = current_site_location
        return current_site_location

    def get(self, **kwargs):
        if 'site' in kwargs:
            site = kwargs['site']
            if not isinstance(site, (int, long, basestring)):
                site = site.id
            site = int(site)
            try:
                return SITE_LOCATION_CACHE[site]
            except KeyError:
                pass
        site_location = models.Manager.get(self, **kwargs)
        SITE_LOCATION_CACHE[site_location.site_id] = site_location
        return site_location

    def clear_cache(self):
        global SITE_LOCATION_CACHE
        SITE_LOCATION_CACHE = {}


class SiteLocation(Thumbnailable):
    """
    An extension to the django.contrib.sites site model, providing
    localtv-specific data.

    Fields:
     - site: A link to the django.contrib.sites.models.Site object
     - logo: custom logo image for this site
     - background: custom background image for this site (unused?)
     - admins: a collection of Users who have access to administrate this
       sitelocation
     - status: one of SiteLocation.STATUS_CHOICES
     - sidebar_html: custom html to appear on the right sidebar of many
       user-facing pages.  Can be whatever's most appropriate for the owners of
       said site.
     - footer_html: HTML that appears at the bottom of most user-facing pages.
       Can be whatever's most appropriate for the owners of said site.
     - about_html: HTML to display on the s about page
     - tagline: displays below the s title on most user-facing pages
     - css: The intention here is to allow  to paste in their own CSS
       here from the admin.  Not used presently, though eventually it should
       be.
     - display_submit_button: whether or not we should allow users to see that
       they can submit videos or not (doesn't affect whether or not they
       actually can though)
     - submission_requires_login: whether or not users need to log in to submit
       videos.
     - tier_name: A short string representing the class of site. This relates to paid extras.
    """
    DISABLED = 0
    ACTIVE = 1

    STATUS_CHOICES = (
        (DISABLED, DISABLED_STATUS_TEXT),
        (ACTIVE, ACTIVE_STATUS_TEXT),
    )

    site = models.ForeignKey(Site, unique=True)
    logo = models.ImageField(upload_to='localtv/site_logos', blank=True)
    background = models.ImageField(upload_to='localtv/site_backgrounds',
                                   blank=True)
    admins = models.ManyToManyField('auth.User', blank=True,
                                    related_name='admin_for')
    status = models.IntegerField(
        choices=STATUS_CHOICES, default=ACTIVE)
    sidebar_html = models.TextField(blank=True)
    footer_html = models.TextField(blank=True)
    about_html = models.TextField(blank=True)
    tagline = models.CharField(max_length=4096, blank=True)
    css = models.TextField(blank=True)
    display_submit_button = models.BooleanField(default=True)
    submission_requires_login = models.BooleanField(default=False)
    playlists_enabled = models.IntegerField(default=1)
    tier_name = models.CharField(max_length=255, default='basic', blank=False, choices=tiers.CHOICES)
    hide_get_started = models.BooleanField(default=False)

    # ordering options
    use_original_date = models.BooleanField(
        default=True,
        help_text="If set, use the original date the video was posted.  "
        "Otherwise, use the date the video was added to this site.")

    # comments options
    screen_all_comments = models.BooleanField(
        verbose_name='Hold comments for moderation',
        default=True,
        help_text="Hold all comments for moderation by default?")
    comments_required_login = models.BooleanField(
        default=False,
        verbose_name="Require Login",
        help_text="If True, comments require the user to be logged in.")

    objects = SiteLocationManager()

    THUMB_SIZES = [
        (88, 68, False),
        (140, 110, False),
        (222, 169, False),
        (130, 110, FORCE_HEIGHT_PADDING) # Facebook
        ]

    class Meta:
        app_label = 'localtv'

    def __unicode__(self):
        return '%s (%s)' % (self.site.name, self.site.domain)

    def add_queued_mail(self, data):
        if not hasattr(self, '_queued_mail'):
            self._queued_mail = []
        self._queued_mail.append(data)

    def get_queued_mail_destructively(self):
        ret = getattr(self, '_queued_mail', [])
        self._queued_mail = []
        return ret

    @staticmethod
    def enforce_tiers(override_setting=None, using='default'):
        '''If the admin has set LOCALTV_DISABLE_TIERS_ENFORCEMENT to a True value,
        then this function returns False. Otherwise, it returns True.'''
        if override_setting is None:
            disabled = DISABLE_TIERS_ENFORCEMENT
        else:
            disabled = override_setting

        if disabled:
            # Well, hmm. If the site admin participated in a PayPal transaction, then we
            # actually will enforce the tiers.
            #
            # Go figure.
            tierdata = TierInfo.objects.db_manager(using).get_current()
            if tierdata.user_has_successfully_performed_a_paypal_transaction:
                return True # enforce it.

        # Generally, we just negate the "disabled" boolean.
        return not disabled

    def user_is_admin(self, user):
        """
        Return True if the given User is an admin for this SiteLocation.
        """
        if not user.is_authenticated() or not user.is_active:
            return False

        if user.is_superuser:
            return True

        return bool(self.admins.filter(pk=user.pk).count())

    def save(self, *args, **kwargs):
        SITE_LOCATION_CACHE[self.site_id] = self
        return models.Model.save(self, *args, **kwargs)

    def get_tier(self):
        return tiers.Tier(self.tier_name, self)

    def get_fully_confirmed_tier(self):
        # If we are in a transitional state, then we would have stored
        # the last fully confirmed tier name in an unusual column.
        tierdata = TierInfo.objects.get_current()
        if tierdata.fully_confirmed_tier_name:
            return tiers.Tier(tierdata.fully_confirmed_tier_name)
        return None

    def get_css_for_display_if_permitted(self):
        '''This function checks the site tier, and if permitted, returns the
        custom CSS the admin has set.

        If that is not permitted, it returns the empty unicode string.'''
        if (not self.enforce_tiers() or
            self.get_tier().permit_custom_css()):
            # Sweet.
            return self.css
        else:
            # Silenced.
            return u''

    def should_show_dashboard(self):
        '''On /admin/, most sites will see a dashboard that gives them
        information at a glance about the site, including its tier status.

        Some sites want to disable that, which they can do by setting the
        LOCALTV_SHOW_ADMIN_DASHBOARD variable to False.

        In that case (in the default theme) the left-hand navigation
        will omit the link to the Dashboard, and also the dashboard itself
        will be an empty page with a META REFRESH that points to
        /admin/approve_reject/.'''
        return SHOW_ADMIN_DASHBOARD

    def should_show_account_level(self):
        '''On /admin/upgrade/, most sites will see an info page that
        shows how to change their account level (AKA site tier).

        Some sites want to disable that, which they can do by setting the
        LOCALTV_SHOW_ADMIN_ACCOUNT_LEVEL variable to False.

        This simply removes the link from the sidebar; if you visit the
        /admin/upgrade/ page, it renders as usual.'''
        return SHOW_ADMIN_ACCOUNT_LEVEL


def finished(sender, **kwargs):
    SiteLocation.objects.clear_cache()
request_finished.connect(finished)

### register pre-save handler for Tiers and payment due dates
models.signals.pre_save.connect(tiers.pre_save_set_payment_due_date,
                                sender=SiteLocation)
models.signals.pre_save.connect(tiers.pre_save_adjust_resource_usage,
                                sender=SiteLocation)
models.signals.post_save.connect(tiers.post_save_send_queued_mail,
                                 sender=SiteLocation)


class SingletonManager(models.Manager):
    def get_current(self):
        current_site_location = SiteLocation._default_manager.db_manager(
            self.db).get_current()
        singleton, created = self.get_or_create(
            sitelocation = current_site_location)
        if created:
            logging.info("Created %s." % self.model.__class__.__name__)
        return singleton


class TierInfo(models.Model):
    payment_due_date = models.DateTimeField(null=True, blank=True)
    free_trial_available = models.BooleanField(default=True)
    free_trial_started_on = models.DateTimeField(null=True, blank=True)
    in_free_trial = models.BooleanField(default=False)
    payment_secret = models.CharField(max_length=255, default='',blank=True) # This is part of payment URLs.
    current_paypal_profile_id = models.CharField(max_length=255, default='',blank=True) # NOTE: When using this, fill it if it seems blank.
    video_allotment_warning_sent = models.BooleanField(default=False)
    free_trial_warning_sent = models.BooleanField(default=False)
    already_sent_welcome_email = models.BooleanField(default=False)
    inactive_site_warning_sent = models.BooleanField(default=False)
    user_has_successfully_performed_a_paypal_transaction = models.BooleanField(default=False)
    already_sent_tiers_compliance_email = models.BooleanField(default=False)
    fully_confirmed_tier_name = models.CharField(max_length=255, default='', blank=True)
    should_send_welcome_email_on_paypal_event = models.BooleanField(default=False)
    waiting_on_payment_until = models.DateTimeField(null=True, blank=True)
    sitelocation = models.OneToOneField('SiteLocation')
    objects = SingletonManager()

    class Meta:
        app_label = 'localtv'

    def get_payment_secret(self):
        '''The secret had better be non-empty. So we make it non-empty right here.'''
        if self.payment_secret:
            return self.payment_secret
        # Guess we had better fill it.
        self.payment_secret = base64.b64encode(os.urandom(16))
        self.save()
        return self.payment_secret

    def site_is_subsidized(self):
        return (self.current_paypal_profile_id == 'subsidized')

    def set_to_subsidized(self):
        if self.current_paypal_profile_id:
            raise AssertionError, (
                "Bailing out: " +
                "the site already has a payment profile configured: %s" %
                                   self.current_paypal_profile_id)
        self.current_paypal_profile_id = 'subsidized'

    def time_until_free_trial_expires(self, now = None):
        if not self.in_free_trial:
            return None
        if not self.payment_due_date:
            return None

        if now is None:
            now = datetime.datetime.utcnow()
        return (self.payment_due_date - now)

    def use_zendesk(self):
        '''If the site is configured to, we can send notifications of
        tiers-related changes to ZenDesk, the customer support ticketing
        system used by PCF.

        A non-PCF deployment of localtv would not want to set the
        LOCALTV_USE_ZENDESK setting. Then this method will return False,
        and the parts of the tiers system that check it will avoid
        making calls out to ZenDesk.'''
        return USE_ZENDESK


# TODO: Move NewsletterSettings into a contrib app. Also, make the videos
# an intermediary model instead of forcing a selection of up to only 5.
class NewsletterSettings(models.Model):
    DISABLED = 0
    FEATURED = 1
    POPULAR = 2
    CUSTOM = 3
    LATEST = 4
    
    STATUS_CHOICES = (
        (DISABLED, DISABLED_STATUS_TEXT),
        (FEATURED, _("5 most recently featured")),
        (POPULAR, _("5 most popular")),
        (LATEST, _("5 latest videos")),
        (CUSTOM, _("Custom selection")),
    )
    sitelocation = models.OneToOneField(SiteLocation)
    status = models.IntegerField(
        choices=STATUS_CHOICES, default=DISABLED,
        help_text='What videos should get sent out in the newsletter?')

    # for custom newsletter
    video1 = models.ForeignKey('localtv.Video', related_name='newsletter1',
                               null=True,
                               help_text='A URL of a video on your site.')
    video2 = models.ForeignKey('localtv.Video', related_name='newsletter2',
                               null=True,
                               help_text='A URL of a video on your site.')
    video3 = models.ForeignKey('localtv.Video', related_name='newsletter3',
                               null=True,
                               help_text='A URL of a video on your site.')
    video4 = models.ForeignKey('localtv.Video', related_name='newsletter4',
                               null=True,
                               help_text='A URL of a video on your site.')
    video5 = models.ForeignKey('localtv.Video', related_name='newsletter5',
                               null=True,
                               help_text='A URL of a video on your site.')
    
    intro = models.CharField(max_length=200, blank=True,
                             help_text=('Include a short introduction to your '
                                        'newsletter. If you will be sending '
                                        'the newsletter automatically, make '
                                        'sure to update this or write '
                                        'something that will be evergreen! '
                                        '(limit 200 characters)'))
    show_icon = models.BooleanField(default=True,
                                    help_text=('Do you want to include your '
                                               'site logo in the newsletter '
                                               'header?'))

    twitter_url = models.URLField(verify_exists=False, blank=True,
                                  help_text='e.g. https://twitter.com/#!/mirocommunity')
    facebook_url = models.URLField(verify_exists=False, blank=True,
                                   help_text='e.g. http://www.facebook.com/universalsubtitles')

    repeat = models.IntegerField(default=0) # hours between sending
    last_sent = models.DateTimeField(null=True)

    objects = SingletonManager()

    class Meta:
        app_label = 'localtv'

    def videos(self):
        if self.status == NewsletterSettings.DISABLED:
            raise ValueError('no videos for disabled newsletter')
        elif self.status == NewsletterSettings.FEATURED:
            videos = Video.objects.get_featured_videos(self.sitelocation)
        elif self.status == NewsletterSettings.POPULAR:
            # popular over the last week
            videos = Video.objects.get_popular_videos(self.sitelocation)
        elif self.status == NewsletterSettings.LATEST:
            videos = Video.objects.get_latest_videos(self.sitelocation)
        elif self.status == NewsletterSettings.CUSTOM:
            videos = [video for video in (
                    self.video1,
                    self.video2,
                    self.video3,
                    self.video4,
                    self.video5) if video]
        return videos[:5]

    def next_send_time(self):
        if not self.repeat:
            return None
        if not self.last_sent:
            dt = datetime.datetime.now()
        else:
            dt = self.last_sent
        return dt + datetime.timedelta(hours=self.repeat)

    def send(self):
        from localtv.admin.user_views import _filter_just_humans
        body = self.as_html()
        subject = '[%s] Newsletter for %s' % (self.sitelocation.site.name,
                                              datetime.datetime.now().strftime('%m/%d/%y'))
        notice_type = notification.NoticeType.objects.get(label='newsletter')
        for u in User.objects.exclude(email=None).exclude(email='').filter(
            _filter_just_humans()):
            if notification.get_notification_setting(u, notice_type, "1"):
                message = EmailMessage(subject, body,
                                       settings.DEFAULT_FROM_EMAIL,
                                       [u.email])
                message.content_subtype = 'html'
                message.send(fail_silently=True)

    def as_html(self, extra_context=None):
        context = {'newsletter': self,
                   'sitelocation': self.sitelocation,
                   'site': self.sitelocation.site}
        if extra_context:
            context.update(extra_context)
        return render_to_string('localtv/admin/newsletter.html',
                                context)


class WidgetSettings(Thumbnailable):
    """
    A Model which represents the options for controlling the widget creator.
    """
    site = models.OneToOneField(Site)

    title = models.CharField(max_length=250, blank=True)
    title_editable = models.BooleanField(default=True)

    icon = models.ImageField(upload_to='localtv/widget_icon', blank=True)
    icon_editable = models.BooleanField(default=False)

    css = models.FileField(upload_to='localtv/widget_css', blank=True)
    css_editable = models.BooleanField(default=False)

    bg_color = models.CharField(max_length=20, blank=True)
    bg_color_editable = models.BooleanField(default=False)

    text_color = models.CharField(max_length=20, blank=True)
    text_color_editable = models.BooleanField(default=False)

    border_color = models.CharField(max_length=20, blank=True)
    border_color_editable = models.BooleanField(default=False)

    THUMB_SIZES = [
        (88, 68, False),
        (140, 110, False),
        (222, 169, False),
        ]

    class Meta:
        app_label = 'localtv'

    def get_title_or_reasonable_default(self):
        # Is the title worth using? If so, use that.
        use_title = True
        if self.title.endswith('example.com'):
            use_title = False
        if not self.title:
            use_title = False

        # Okay, so either we return the title, or a sensible default
        if use_title:
            return django.utils.html.escape(self.title)
        return self.generate_reasonable_default_title()

    def generate_reasonable_default_title(self):
        prefix = 'Watch Videos on %s'

        # Now, work on calculating what goes at the end.
        site = Site.objects.get_current()

        # The default suffix is a self-link. If the site name and
        # site domain are plausible, do that.
        if ((site.name and site.name.lower() != 'example.com') and
            (site.domain and site.domain.lower() != 'example.com')):
            suffix = '<a href="http://%s/">%s</a>' % (
                site.domain, django.utils.html.escape(site.name))

        # First, we try the site name, if that's a nice string.
        elif site.name and site.name.lower() != 'example.com':
            suffix = site.name

        # Else, we try the site domain, if that's not example.com
        elif site.domain.lower() != 'example.com':
            suffix = site.domain

        else:
            suffix = 'our video site'

        return prefix % suffix
