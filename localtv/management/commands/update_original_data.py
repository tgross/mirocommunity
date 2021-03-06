# Miro Community - Easiest way to make a video website
#
# Copyright (C) 2010, 2011, 2012 Participatory Culture Foundation
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

import traceback

from django.core.management.base import NoArgsCommand
from localtv.management import site_too_old
from localtv.models import Video, OriginalVideo
import vidscraper.errors

class Command(NoArgsCommand):

    args = ''

    def handle_noargs(self, **options):
        if site_too_old():
            return
        for original in OriginalVideo.objects.exclude(
            video__status=Video.REJECTED):
            try:
                original.update()
            except vidscraper.errors.CantIdentifyUrl, e:
                pass # It is okay if we cannot update a remote video. No need to be noisy.
            except Exception:
                traceback.print_exc()
