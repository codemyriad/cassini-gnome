# SPDX-License-Identifier: GPL-3.0-or-later
"""Product identity and current preferences; no development-build migrations."""

from gi.repository import Gio

APP_ID = "io.codemyriad.CassiniTalk"


def settings(*, source=None, backend=None):
    source = source or Gio.SettingsSchemaSource.get_default()
    return Gio.Settings.new_full(source.lookup(APP_ID, True), backend, None)


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
