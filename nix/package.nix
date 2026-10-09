{ python3Packages, lib, ffmpeg }:
python3Packages.buildPythonApplication {
  pname = "ctx-agent";
  version = "0.1.0";
  src = ../.;
  pyproject = true;
  build-system = [ python3Packages.setuptools ];
  pythonImportsCheck = [ "ctx_agent" ];
  makeWrapperArgs = [ "--prefix PATH : ${lib.makeBinPath [ ffmpeg ]}" ];
  doCheck = false;
}
