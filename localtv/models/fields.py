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

import bitly
from django.conf import settings
from django.db import models


class BitLyWrappingURLField(models.URLField):
    def get_db_prep_value(self, value, *args, **kwargs):
        if not getattr(settings, 'BITLY_LOGIN'):
            return value

        # Workaround for some cases
        if value is None:
            value = ''

        if len(value) <= self.max_length: # short enough to save
            return value
        api = bitly.Api(login=settings.BITLY_LOGIN,
                        apikey=settings.BITLY_API_KEY)
        try:
            return unicode(api.shorten(value))
        except bitly.BitlyError:
            return unicode(value)[:self.max_length]


try:
    from south.modelsinspector import add_introspection_rules
except ImportError:
    pass
else:
    add_introspection_rules([], ["^localtv\.models\.BitLyWrappingURLField"])