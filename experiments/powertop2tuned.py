#!/usr/bin/python -Es
# -*- coding: utf-8 -*-
#
# Copyright (C) 2008-2013 Red Hat, Inc.
# Authors: Jan Kaluža <jkaluza@redhat.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#

from __future__ import print_function
# exception handler for python 2/3 compatibility
try:
	from builtins import chr
except ImportError:
	pass
import os
import sys
import tempfile
import shutil
import argparse
import codecs
from subprocess import *
# exception handler for python 2/3 compatibility
try:
	from html.parser import HTMLParser
	from html.entities import name2codepoint
except ImportError:
	from HTMLParser import HTMLParser
	from htmlentitydefs import name2codepoint


SCRIPT_SH = """#!/bin/sh

. /usr/lib/tuned/functions

start() {
%s
	return 0
}

stop() {
%s
	return 0
}

process $@
"""

TUNED_CONF_PROLOG = "# Automatically generated by powertop2tuned tool\n\n"
TUNED_CONF_INCLUDE = """[main]
%s\n
"""
TUNED_CONF_EPILOG="""\n[powertop_script]
type=script
replace=1
script=script.sh
"""


class PowertopHTMLParser(HTMLParser):
	def __init__(self, enable_tunings):
		HTMLParser.__init__(self)

		self.inProperTable = False
		self.inScript = False
		self.intd = False
		self.lastStartTag = ""
		self.tdCounter = 0
		self.lastDesc = ""
		self.data = ""
		self.currentScript = ""
		if enable_tunings:
			self.prefix = ""
		else:
			self.prefix = "#"

		self.plugins = {}

	def getParsedData(self):
		return self.data

	def getPlugins(self):
		return self.plugins

	def handle_starttag(self, tag, attrs):
		self.lastStartTag = tag
		if self.lastStartTag == "div" and dict(attrs).get("id")  == "tuning":
			self.inProperTable = True
		if self.inProperTable and tag == "td":
			self.tdCounter += 1
			self.intd = True

	def parse_command(self, command):
		prefix = ""
		command = command.strip()
		if command[0] == '#':
			prefix = "#"
			command = command[1:]

		if command.startswith("echo") and command.find("/proc/sys") != -1:
			splitted = command.split("'")
			value = splitted[1]
			path = splitted[3]
			path = path.replace("/proc/sys/", "").replace("/", ".")
			self.plugins.setdefault("sysctl", "[sysctl]\n")
			self.plugins["sysctl"] += "#%s\n%s%s=%s\n\n" % (self.lastDesc, prefix, path, value)
		# TODO: plugins/plugin_sysfs.py doesn't support this so far, it has to be implemented to 
		# let it work properly.
		elif command.startswith("echo") and (command.find("'/sys/") != -1 or command.find("\"/sys/") != -1):
			splitted = command.split("'")
			value = splitted[1]
			path = splitted[3]
			if path in ("/sys/module/snd_hda_intel/parameters/power_save", "/sys/module/snd_ac97_codec/parameters/power_save"):
				self.plugins.setdefault("audio", "[audio]\n")
				self.plugins["audio"] += "#%s\n%stimeout=1\n" % (self.lastDesc, prefix)
			else:
				self.plugins.setdefault("sysfs", "[sysfs]\n")
				self.plugins["sysfs"] += "#%s\n%s%s=%s\n\n" % (self.lastDesc, prefix, path, value)
		elif command.startswith("ethtool -s ") and command.endswith("wol d;"):
			self.plugins.setdefault("net", "[net]\n")
			self.plugins["net"] += "#%s\n%swake_on_lan=0\n" % (self.lastDesc, prefix)
		else:
			return False
		return True

	def handle_endtag(self, tag):
		if self.inProperTable and tag == "table":
			self.inProperTable = False
			self.intd = False
		if tag == "tr":
			self.tdCounter = 0
			self.intd = False
		if tag == "td":
			self.intd = False
		if self.inScript:
			#print self.currentScript
			self.inScript = False
			# Command is not handled, so just store it in the script
			if not self.parse_command(self.currentScript.split("\n")[-1]):
				self.data += self.currentScript + "\n\n"

	def handle_entityref(self, name):
		if self.inScript:
			self.currentScript += chr(name2codepoint[name])

	def handle_data(self, data):
		prefix = self.prefix
		if self.inProperTable and self.intd and self.tdCounter == 1:
			self.lastDesc = data
			if self.lastDesc.lower().find("autosuspend") != -1 and (self.lastDesc.lower().find("keyboard") != -1 or self.lastDesc.lower().find("mouse") != -1):
					self.lastDesc += "\n# WARNING: For some devices, uncommenting this command can disable the device."
					prefix = "#"
		if self.intd and ((self.inProperTable and self.tdCounter == 2) or self.inScript):
			self.tdCounter = 0
			if not self.inScript:
				self.currentScript += "\t# " + self.lastDesc + "\n"
				self.currentScript += "\t" + prefix + data.strip()
				self.inScript = True
			else:
				self.currentScript += data.strip()

class PowertopProfile:
	BAD_PRIVS = 100
	PARSING_ERROR = 101
	BAD_SCRIPTSH = 102

	def __init__(self, output, profile_name, name = ""):
		self.profile_name = profile_name
		self.name = name
		self.output = output

	def currentActiveProfile(self):
		proc = Popen(["tuned-adm", "active"], stdout=PIPE, \
				universal_newlines = True)
		output = proc.communicate()[0]
		if output and output.find("Current active profile: ") == 0:
			return output[len("Current active profile: "):output.find("\n")]
		return None

	def checkPrivs(self):
		myuid = os.geteuid()
		if myuid != 0:
			print('Run this program as root', file=sys.stderr)
			return False
		return True

	def generateHTML(self):
		print("Running PowerTOP, please wait...")
		environment = os.environ.copy()
		environment["LC_ALL"] = "C"
		try:
			proc = Popen(["/usr/sbin/powertop", \
					"--html=/tmp/powertop", "--time=1"], \
					stdout=PIPE, stderr=PIPE, \
					env=environment, \
					universal_newlines = True)
			output = proc.communicate()[1]
		except (OSError, IOError):
			print('Unable to execute PowerTOP, is PowerTOP installed?', file=sys.stderr)
			return -2

		if proc.returncode != 0:
			print('PowerTOP returned error code: %d' % proc.returncode, file=sys.stderr)
			return -2

		prefix = "PowerTOP outputing using base filename "
		if output.find(prefix) == -1:
			return -1

		name = output[output.find(prefix)+len(prefix):-1]
		#print "Parsed filename=", [name]
		return name

	def parseHTML(self, enable_tunings):
		f = None
		data = None
		parser = PowertopHTMLParser(enable_tunings)
		try:
			f = codecs.open(self.name, "r", "utf-8")
			data = f.read()
		except (OSError, IOError, UnicodeDecodeError):
			data = None

		if f is not None:
			f.close()

		if data is None:
			return "", ""

		parser.feed(data)
		return parser.getParsedData(), parser.getPlugins()

	def generateShellScript(self, data):
		print("Generating shell script", os.path.join(self.output, "script.sh"))
		f = None
		try:
			f = codecs.open(os.path.join(self.output, "script.sh"), "w", "utf-8")
			f.write(SCRIPT_SH % (data, ""))
			os.fchmod(f.fileno(), 0o755)
			f.close()
		except (OSError, IOError) as e:
			print("Error writing shell script: %s" % e, file=sys.stderr)
			if f is not None:
				f.close()
			return False
		return True

	def generateTunedConf(self, profile, plugins):
		print("Generating Tuned config file", os.path.join(self.output, "tuned.conf"))
		f = codecs.open(os.path.join(self.output, "tuned.conf"), "w", "utf-8")
		f.write(TUNED_CONF_PROLOG)
		if profile is not None:
			if self.profile_name == profile:
				print('New profile has same name as active profile, not including active profile (avoiding circular deps).', file=sys.stderr)
			else:
				f.write(TUNED_CONF_INCLUDE % ("include=" + profile))

		for plugin in list(plugins.values()):
			f.write(plugin + "\n")

		f.write(TUNED_CONF_EPILOG)
		f.close()

	def generate(self, new_profile, merge_profile, enable_tunings):
		generated_html = False
		if len(self.name) == 0:
			generated_html = True
			if not self.checkPrivs():
				return self.BAD_PRIVS

			name = self.generateHTML()
			if isinstance(name, int):
				return name
			self.name = name

		data, plugins = self.parseHTML(enable_tunings)

		if generated_html:
			os.unlink(self.name)

		if len(data) == 0 and len(plugins) == 0:
			print('Your Powertop version is incompatible (maybe too old) or the generated HTML output is malformed', file=sys.stderr)
			return self.PARSING_ERROR

		if new_profile is False:
			if merge_profile is None:
				profile = self.currentActiveProfile()
			else:
				profile = merge_profile
		else:
			profile = None

		if not os.path.exists(self.output):
			os.makedirs(self.output)

		if not self.generateShellScript(data):
			return self.BAD_SCRIPTSH

		self.generateTunedConf(profile, plugins)

		return 0

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description='Creates Tuned profile from Powertop HTML output.')
	parser.add_argument('profile', metavar='profile_name', type=str, nargs='?', help='Name for the profile to be written.')
	parser.add_argument('-i', '--input', metavar='input_html', type=str, help='Path to Powertop HTML report. If not given, it is generated automatically.')
	parser.add_argument('-o', '--output', metavar='output_directory', type=str, help='Directory where the profile will be written, default is /etc/tuned/profile_name directory.')
	parser.add_argument('-n', '--new-profile', action='store_true', help='Creates new profile, otherwise it merges (include) your current profile.')
	parser.add_argument('-m', '--merge-profile', action = 'store', help = 'Merges (includes) the specified profile (can be suppressed by -n option).')
	parser.add_argument('-f', '--force', action='store_true', help='Overwrites the output directory if it already exists.')
	parser.add_argument('-e', '--enable', action='store_true', help='Enable all tunings (not recommended). Even with this enabled tunings known to be harmful (like USB_AUTOSUSPEND) won''t be enabled.')
	args = parser.parse_args()
	args = vars(args)

	if not args['profile'] and not args['output']:
		print('You have to specify the profile_name or output directory using the --output argument.', file=sys.stderr)
		parser.print_help()
		sys.exit(-1)

	if not args['output']:
		args['output'] = "/etc/tuned"

	if args['profile']:
		args['output'] = os.path.join(args['output'], args['profile'])

	if not args['input']:
		args['input'] = ''

	if os.path.exists(args['output']) and not args['force']:
		print('Output directory already exists, use --force to overwrite it.', file=sys.stderr)
		sys.exit(-1)

	p = PowertopProfile(args['output'], args['profile'], args['input'])
	sys.exit(p.generate(args['new_profile'], args['merge_profile'], args['enable']))
