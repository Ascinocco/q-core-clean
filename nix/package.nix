# The q-core app: its source plus an offline virtualenv built from the
# hash-pinned requirements.lock.
#
# requirements.txt states floors, and the MCP SDK's 2.x line isn't in
# nixpkgs, so the dependencies can't come from python3Packages. Instead the
# wheels named in the lock are fetched once, as a fixed-output derivation
# whose hash is recorded per system in wheelhouse-hashes.nix, and installed
# with --no-index --require-hashes. Regenerate both after changing
# api/requirements.txt (runbooks/deploy-nixos.md).
{ lib, stdenv, stdenvNoCC, python313, cacert, autoPatchelfHook, zlib, wheelhouseHash }:
let
  python = python313;
  lock = ../requirements.lock;

  wheelhouse = stdenvNoCC.mkDerivation {
    name = "q-core-wheelhouse";
    nativeBuildInputs = [ python cacert ];
    dontUnpack = true;
    buildPhase = ''
      export HOME=$TMPDIR
      ${python}/bin/python -m venv $TMPDIR/pip
      $TMPDIR/pip/bin/python -m pip download --quiet --no-deps --only-binary=:all: \
        --require-hashes -r ${lock} -d $out
    '';
    dontInstall = true;
    outputHashMode = "recursive";
    outputHashAlgo = "sha256";
    outputHash = wheelhouseHash;
  };

  venv = stdenv.mkDerivation {
    name = "q-core-venv";
    dontUnpack = true;
    nativeBuildInputs = [ python autoPatchelfHook ];
    buildInputs = [ stdenv.cc.cc.lib zlib ];
    buildPhase = ''
      export HOME=$TMPDIR
      ${python}/bin/python -m venv $out
      $out/bin/python -m pip install --quiet --no-index --no-deps --require-hashes \
        --no-compile --find-links ${wheelhouse} -r ${lock}
      $out/bin/python -m pip uninstall --quiet --yes pip
      $out/bin/python -m compileall -q $out/lib
    '';
    dontInstall = true;
  };

  app = stdenvNoCC.mkDerivation {
    pname = "q-core";
    version = "0";
    src = lib.fileset.toSource {
      root = ../.;
      fileset = lib.fileset.difference
        (lib.fileset.unions [ ../api ../q_core_mcp ../db ../financial_evals ])
        (lib.fileset.unions [ ../api/tests ../q_core_mcp/tests ]);
    };
    dontBuild = true;
    installPhase = ''
      mkdir -p $out
      cp -r api q_core_mcp db financial_evals $out/
      ln -s ${venv} $out/.venv
      ${venv}/bin/python -m compileall -q $out/api $out/q_core_mcp $out/financial_evals
    '';
    passthru = { inherit python venv wheelhouse; };
    meta.description = "q-core API, MCP server and database migrations";
  };
in
app
