{ python3Packages, lib, ffmpeg, codex, openssh }:
python3Packages.buildPythonApplication {
  pname = "ctx-agent";
  version = "0.3.0";
  src = ../.;
  pyproject = true;
  build-system = [ python3Packages.setuptools ];
  pythonImportsCheck = [ "ctx_agent" ];
  makeWrapperArgs = [
    "--prefix PATH : ${lib.makeBinPath [ ffmpeg codex openssh ]}"
  ];
  doCheck = false;
}
