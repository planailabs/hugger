{ lib
, buildPythonApplication
, hatchling
, fasthtml
, huggingface-hub
, yoyo-migrations
, argon2-cffi
, uvicorn
}:

buildPythonApplication {
  pname = "hugger";
  version = "0.1.0";
  pyproject = true;

  # Repo root, minus dev/build artifacts that shouldn't enter the store.
  src = lib.cleanSourceWith {
    src = lib.cleanSource ../.;
    filter = path: _type:
      let b = baseNameOf path;
      in !(builtins.elem b [ ".venv" "result" ".hugger" ]);
  };

  build-system = [ hatchling ];

  dependencies = [
    fasthtml
    huggingface-hub
    yoyo-migrations
    argon2-cffi
    uvicorn
  ];

  # Importing the package is cheap and side-effect free (config/app are not
  # imported by __init__, so no state dirs are created at build time).
  pythonImportsCheck = [ "hugger" ];
  doCheck = false;

  meta = {
    description = "A HuggingFace model archiver (web UI + browser extension)";
    mainProgram = "hugger";
    license = lib.licenses.mit;
  };
}
