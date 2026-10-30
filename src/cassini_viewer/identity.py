# SPDX-License-Identifier: GPL-3.0-or-later
"""D45 identity and first-run GSettings migration; private files stay in place."""

import logging

from gi.repository import Gio

APP_ID = "io.codemyriad.CassiniTalk"
LEGACY_APP_ID = "com.gocassini.Viewer"
MIGRATED = "legacy-settings-migrated"
LOG = logging.getLogger(__name__)
# These are confirmations for the old application identity, not preferences.
# The new identity requests its own portal permissions and explains its route.
RESET = frozenset({"background-notification-shown"})


def settings(*, source=None, backend=None):
    source = source or Gio.SettingsSchemaSource.get_default()
    schema = source.lookup(APP_ID, True)
    current = Gio.Settings.new_full(schema, backend, None)
    if current.get_boolean(MIGRATED):
        return current
    previous = source.lookup(LEGACY_APP_ID, True)
    if previous is None:
        return current  # Retry when compatibility schemas become available.
    old = Gio.Settings.new_full(previous, backend, None)
    current.delay()
    for key in sorted(set(schema.list_keys()) & set(previous.list_keys()) - RESET):
        value = old.get_user_value(key)
        if value is None or current.get_user_value(key) is not None or not current.is_writable(key):
            continue
        if value.get_type_string() != schema.get_key(key).get_value_type().dup_string():
            continue
        if not current.set_value(key, value):
            current.revert()
            LOG.warning("Could not migrate Cassini settings; the previous values are kept")
            return current
    if not current.set_boolean(MIGRATED, True):
        current.revert()
        return current
    current.apply()
    Gio.Settings.sync()
    return current


def product_menu(model):
    """Keep native translated menu wording, using the catalog's product name."""
    from .strings import text

    menu = Gio.Menu()
    for index in range(model.get_n_items()):
        item = Gio.MenuItem.new_from_model(model, index)
        label = model.get_item_attribute_value(index, "label", None)
        if label is not None:
            item.set_label(label.get_string().replace("Cassini", text("app.name")))
        for link in ("section", "submenu"):
            child = model.get_item_link(index, link)
            if child is not None:
                item.set_link(link, product_menu(child))
        menu.append_item(item)
    return menu
