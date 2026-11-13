{
  lib,
  anki-utils,
}:
anki-utils.buildAnkiAddon {
  # Also the add-on's folder/module name; keep it stable so config carries over.
  pname = "anki_ambush";
  version = "1.0.0";

  src = lib.cleanSourceWith {
    src = ../anki_ambush;
    filter =
      path: _type:
      !(builtins.elem (baseNameOf path) [
        "__pycache__"
        "meta.json"
      ]);
  };

  meta = {
    description = "Anki add-on that pops up a few due cards in a small always-on-top window at random times";
    platforms = lib.platforms.all;
  };
}
