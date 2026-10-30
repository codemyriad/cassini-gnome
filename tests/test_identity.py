# SPDX-License-Identifier: GPL-3.0-or-later
"""D45 copies user preferences once without overwriting the new identity."""

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("gi.repository.Gio")
from gi.repository import Gio

from cassini_viewer.identity import APP_ID, LEGACY_APP_ID, MIGRATED, settings


@pytest.fixture
def preferences(tmp_path):
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    source = Path(__file__).parents[1] / "data/io.codemyriad.CassiniTalk.gschema.xml"
    (schemas / source.name).write_bytes(source.read_bytes())
    legacy = source.with_name("com.gocassini.Viewer.gschema.xml")
    (schemas / legacy.name).write_bytes(legacy.read_bytes())
    subprocess.run(["glib-compile-schemas", "--strict", str(schemas)], check=True)
    schema_source = Gio.SettingsSchemaSource.new_from_directory(str(schemas), None, False)
    backend = Gio.keyfile_settings_backend_new(str(tmp_path / "preferences.ini"), "/", None)
    old = Gio.Settings.new_full(schema_source.lookup(LEGACY_APP_ID, False), backend, None)
    new = Gio.Settings.new_full(schema_source.lookup(APP_ID, False), backend, None)
    return schema_source, backend, old, new


def test_migration_preserves_explicit_types_and_leaves_old_values_untouched(preferences):
    source, backend, old, new = preferences
    old.set_int("window-width", 935)
    old.set_boolean("record-shortcut-enabled", True)
    old.set_string("export-folder-uri", "file:///home/example/Music/Exports")
    old.set_string("speech-model", "parakeet-ultra-0.6b-int8")
    migrated = settings(source=source, backend=backend)
    assert migrated.get_boolean(MIGRATED)
    for key in ("window-width", "record-shortcut-enabled", "export-folder-uri", "speech-model"):
        assert migrated.get_value(key) == old.get_value(key)
    assert new.get_int("window-width") == 935
    assert old.get_int("window-width") == 935
    assert new.get_user_value("window-height") is None, "Defaults must remain defaults"


def test_migration_keeps_values_already_saved_under_the_new_id_and_never_replays(preferences):
    source, backend, old, new = preferences
    old.set_int("window-width", 935)
    new.set_int("window-width", 1024)
    settings(source=source, backend=backend)
    assert new.get_int("window-width") == 1024
    old.set_int("window-height", 888)
    settings(source=source, backend=backend)
    assert new.get_user_value("window-height") is None
    assert new.get_boolean(MIGRATED)


def test_preferences_changed_after_migration_are_immediately_persisted(preferences):
    source, backend, old, new = preferences
    old.set_int("window-width", 935)
    migrated = settings(source=source, backend=backend)
    migrated.set_boolean("follow-along", False)
    Gio.Settings.sync()
    assert not new.get_boolean("follow-along"), "Other Settings instances must see later edits"
    assert not settings(source=source, backend=backend).get_boolean("follow-along")


def test_legacy_native_menus_keep_actions_and_use_the_catalog_product_name():
    from cassini_viewer.identity import product_menu

    child = Gio.Menu()
    child.append("_About Cassini", "app.about")
    model = Gio.Menu()
    model.append_section(None, child)
    renamed = product_menu(model).get_item_link(0, "section")
    assert renamed.get_item_attribute_value(0, "label", None).get_string() == "_About Cassini talk"
    assert renamed.get_item_attribute_value(0, "action", None).get_string() == "app.about"
    assert child.get_item_attribute_value(0, "label", None).get_string() == "_About Cassini"
