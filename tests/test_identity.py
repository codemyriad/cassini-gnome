# SPDX-License-Identifier: GPL-3.0-or-later
"""Fresh current-identity settings persist without development-build migration."""

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("gi.repository.Gio")
from gi.repository import Gio

from cassini_talk.identity import APP_ID, settings


def test_fresh_preferences_use_only_the_current_schema_and_persist(tmp_path):
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    source = Path(__file__).parents[1] / "data/io.codemyriad.CassiniTalk.gschema.xml"
    (schemas / source.name).write_bytes(source.read_bytes())
    subprocess.run(["glib-compile-schemas", "--strict", str(schemas)], check=True)
    schema_source = Gio.SettingsSchemaSource.new_from_directory(str(schemas), None, False)
    backend = Gio.keyfile_settings_backend_new(str(tmp_path / "preferences.ini"), "/", None)
    current = settings(source=schema_source, backend=backend)
    assert current.props.schema_id == APP_ID
    assert current.get_user_value("window-width") is None
    current.set_int("window-width", 935)
    current.set_boolean("follow-along", False)
    Gio.Settings.sync()
    other = settings(source=schema_source, backend=backend)
    assert other.get_int("window-width") == 935 and not other.get_boolean("follow-along")


def test_product_menus_keep_actions_and_use_the_catalog_product_name():
    from cassini_talk.identity import product_menu

    child = Gio.Menu()
    child.append("_About Cassini", "app.about")
    model = Gio.Menu()
    model.append_section(None, child)
    renamed = product_menu(model).get_item_link(0, "section")
    assert renamed.get_item_attribute_value(0, "label", None).get_string() == "_About Cassini talk"
    assert renamed.get_item_attribute_value(0, "action", None).get_string() == "app.about"
    assert child.get_item_attribute_value(0, "label", None).get_string() == "_About Cassini"
