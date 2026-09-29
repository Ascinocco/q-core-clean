# The wheelhouse's fixed-output hash per system. After changing
# requirements.lock, set the entry to lib.fakeHash, build, and copy the hash
# Nix reports (runbooks/deploy-nixos.md).
{
  x86_64-linux = "sha256-jsv9fvFi2B/8nQfuiKq349ThunMpGws29NEUzRpt5qw=";
  aarch64-linux = "sha256-w1QXt9RPZc4uiXvqMGTDpFRyQnFJgGVYHzI4w0mEVK8=";
}
