# Miro Community - Easiest way to make a video website
#
# Copyright (C) 2011, 2012 Participatory Culture Foundation
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

import tempfile
import os
import shutil

import django.conf

from django.core.management.base import NoArgsCommand

import haystack.management.commands.update_index

class Command(NoArgsCommand):

    args = ''

    def handle_noargs(self, **options):
        # This just calls update_index(), but it temporarily
        # overrides the XAPIAN_INDEX path to a path in /tmp.
        #
        # This avoids NFS locking during the Xapian index process.
        old_xapian_path = django.conf.settings.HAYSTACK_XAPIAN_PATH
        if old_xapian_path.endswith('/'):
            old_xapian_path = old_xapian_path[:-1]

        tmpdir = tempfile.mkdtemp(dir='/tmp/')
        django.conf.settings.HAYSTACK_XAPIAN_PATH = tmpdir
        cmd = haystack.management.commands.update_index.Command()
        cmd.handle()
        # If we get this far, move the tmpdir to the real path
        new_path = old_xapian_path + ('.tmp.%d' % os.getpid())
        assert not os.path.exists(new_path)
        shutil.move(tmpdir, new_path)
        os.rename(old_xapian_path, old_xapian_path + '.old')
        os.rename(new_path, old_xapian_path)
        shutil.rmtree(old_xapian_path + '.old')

