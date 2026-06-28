{ config, lib, pkgs, ... }:

let
  cfg = config.services.hugger;
in
{
  options.services.hugger = {
    enable = lib.mkEnableOption "hugger, a HuggingFace model archiver";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.python3Packages.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.python3Packages.callPackage ./package.nix { }";
      description = "The hugger package to run.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address to bind. Use 0.0.0.0 only behind a TLS reverse proxy.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 7860;
      description = "Port to listen on.";
    };

    archiveDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/hugger/archives";
      description = "Where model snapshots are stored.";
    };

    httpsOnly = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Send HSTS headers (enable when served over HTTPS).";
    };

    allowedHosts = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "*" ];
      description = "Permitted Host headers. Set your domain when publicly exposed.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the listen port in the firewall.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/hugger.env";
      description = ''
        Path to an EnvironmentFile (KEY=VALUE lines) for secrets, e.g.
        HUGGER_PASSWORD=... and HF_TOKEN=.... Without HUGGER_PASSWORD, hugger
        generates an initial password (printed to the journal) and forces a
        change on first login.
      '';
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Extra environment variables for the service (e.g. SSL_CERT_FILE).";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.hugger = {
      isSystemUser = true;
      group = "hugger";
      home = "/var/lib/hugger";
    };
    users.groups.hugger = { };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    systemd.services.hugger = {
      description = "hugger — HuggingFace model archiver";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment = {
        HUGGER_HOME = "/var/lib/hugger";
        HUGGER_ARCHIVE_DIR = cfg.archiveDir;
        HUGGER_HOST = cfg.host;
        HUGGER_PORT = toString cfg.port;
        HUGGER_HTTPS_ONLY = lib.boolToString cfg.httpsOnly;
        HUGGER_ALLOWED_HOSTS = lib.concatStringsSep "," cfg.allowedHosts;
      } // cfg.extraEnvironment;

      serviceConfig = {
        ExecStart = lib.getExe cfg.package;
        User = "hugger";
        Group = "hugger";
        StateDirectory = "hugger";
        StateDirectoryMode = "0700";
        Restart = "on-failure";
        EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;

        # Hardening.
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" ];
        RestrictNamespaces = true;
        LockPersonality = true;
      };
    };
  };
}
