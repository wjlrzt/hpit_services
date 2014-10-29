"""
This script is used to start the Flask server through uWSGI on *nix systems.
"""

import os
from management.settings_manager import SettingsManager

try:
    settings_manager = SettingsManager.init_instance(os.environ['HPIT_ENV'])
except KeyError:
    settings_manager = SettingsManager.init_instance('debug')

from server.views.api import *
from server.views.dashboard import *
from server.app import ServerApp

server_app = ServerApp.get_instance()
server_app.bootstrap_user()

app = server_app.app