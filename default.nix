{
  lib,
  python314Packages,
  git,
}:

python314Packages.buildPythonApplication {
  pname = "codexctl";
  version = "0.1.0";

  pyproject = true;

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./README.md
      ./LICENSE
      ./src
      ./tests
      ./scripts
      ./examples
    ];
  };

  build-system = with python314Packages; [
    hatchling
  ];

  dependencies = with python314Packages; [
    websockets
  ];

  nativeCheckInputs =
    with python314Packages;
    [
      pytestCheckHook
      pytest-asyncio
    ]
    ++ [ git ];

  pythonImportsCheck = [
    "codexctl"
  ];

  meta = {
    description = "Command-line interface for starting, resuming, observing, steering, and interrupting Codex threads through a shared Codex runtime.";
    license = lib.licenses.asl20;
    mainProgram = "codexctl";
  };
}
