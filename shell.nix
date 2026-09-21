{ pkgs ? import <nixpkgs> {} }:

with pkgs;

mkShell {
  buildInputs = [
    (python3.withPackages (pp: with pp; [
      psutil
    ]))
  ];
}
