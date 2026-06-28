# NixOS VM test: load the extension into real Chromium and Firefox (temp
# profiles), confirm it injects its UI on a HuggingFace model page, and exercise
# the archive -> remove cycle end-to-end through the server + fake hub.
{ pkgs, self }:

let
  certs = pkgs.runCommand "hugger-test-certs" { nativeBuildInputs = [ pkgs.openssl ]; } ''
    mkdir -p $out
    openssl req -x509 -newkey rsa:2048 -nodes -keyout $out/ca.key -out $out/ca.crt \
      -subj "/CN=hugger Test CA" -days 3650 \
      -addext "basicConstraints=critical,CA:TRUE" \
      -addext "keyUsage=critical,keyCertSign,cRLSign"
    openssl req -newkey rsa:2048 -nodes -keyout $out/server.key -out $out/server.csr \
      -subj "/CN=huggingface.co" -addext "subjectAltName=DNS:huggingface.co"
    openssl x509 -req -in $out/server.csr -CA $out/ca.crt -CAkey $out/ca.key -CAcreateserial \
      -out $out/server.crt -days 3650 \
      -extfile <(printf "subjectAltName=DNS:huggingface.co\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth")
  '';

  pyEnv = pkgs.python3.withPackages (ps: [ ps.selenium ]);
in
pkgs.testers.runNixOSTest {
  name = "hugger-browser";

  nodes.machine = { config, pkgs, ... }: {
    imports = [ self.nixosModules.default ];

    services.hugger = {
      enable = true;
      host = "127.0.0.1";
      port = 7860;
      extraEnvironment = {
        SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
        HF_HUB_DOWNLOAD_TIMEOUT = "30";
      };
    };

    security.pki.certificateFiles = [ "${certs}/ca.crt" ];
    networking.hosts."127.0.0.1" = [ "huggingface.co" ];

    systemd.services.fake-hf-hub = {
      description = "Fake HuggingFace Hub (HTTPS)";
      wantedBy = [ "multi-user.target" ];
      before = [ "hugger.service" ];
      environment = {
        FAKE_HUB_HOST = "0.0.0.0";
        FAKE_HUB_PORT = "443";
        FAKE_HUB_CERT = "${certs}/server.crt";
        FAKE_HUB_KEY = "${certs}/server.key";
      };
      serviceConfig = {
        ExecStart = "${pkgs.python3}/bin/python ${./fake_hf_hub.py}";
        DynamicUser = true;
        AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ];
      };
    };

    environment.systemPackages = [ pkgs.curl pkgs.jq pkgs.zip pyEnv ];
    virtualisation.memorySize = 4096;
    virtualisation.cores = 2;
  };

  testScript = ''
    start_all()
    machine.wait_for_unit("fake-hf-hub.service")
    machine.wait_for_open_port(443)
    machine.wait_for_unit("hugger.service")
    machine.wait_for_open_port(7860)

    token = machine.succeed("jq -r .api_token /var/lib/hugger/config.json").strip()

    # Stage the extension with the token baked into defaults.js (no popup clicks),
    # and a zipped copy for Firefox's temporary add-on installer.
    machine.succeed("cp -r ${../extension} /tmp/ext && chmod -R u+w /tmp/ext")
    machine.succeed(
        "printf 'self.HUGGER_DEFAULTS = { serverUrl: \"http://127.0.0.1:7860\", token: \"%s\" };\\n' "
        f"'{token}' > /tmp/ext/defaults.js"
    )
    machine.succeed("cd /tmp/ext && zip -q -r /tmp/ext.xpi .")

    env = (
        f"HOME=/root HUGGER_TOKEN={token} HUGGER_SERVER=http://127.0.0.1:7860 "
        "HUGGER_EXT_DIR=/tmp/ext HUGGER_XPI=/tmp/ext.xpi "
        "CHROME_BIN=${pkgs.chromium}/bin/chromium CHROMEDRIVER=${pkgs.chromedriver}/bin/chromedriver "
        "FIREFOX_BIN=${pkgs.firefox}/bin/firefox GECKODRIVER=${pkgs.geckodriver}/bin/geckodriver"
    )

    with subtest("chromium"):
        print(machine.succeed(f"{env} ${pyEnv}/bin/python ${./browser_check.py} chrome", timeout=300))

    with subtest("firefox"):
        print(machine.succeed(f"{env} ${pyEnv}/bin/python ${./browser_check.py} firefox", timeout=300))
  '';
}
