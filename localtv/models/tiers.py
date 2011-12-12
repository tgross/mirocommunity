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
import base64
import os

from django.conf import settings
from django.db import models


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
    sitelocation = models.OneToOneField('localtv.SiteLocation')
    objects = SingletonManager()

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
        return getattr(settings, 'LOCALTV_USE_ZENDESK', False)