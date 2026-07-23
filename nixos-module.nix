{ self, ... }:
{
  lib,
  pkgs,
  config,
  ...
}:

let
  settingsFormat = pkgs.formats.yaml { };
  defaultUser = "navidrome-collector";
in
{
  options = {
    services.navidrome-collector = {
      enable = lib.mkEnableOption "Navidrome Music Collector";

      package = lib.mkPackageOption pkgs "navidrome-collector" {
        default = [ "navidrome-collector" ];
      };

      user = lib.mkOption {
        type = lib.types.str;
        default = defaultUser;
        description = "User account under which the collector runs.";
      };

      group = lib.mkOption {
        type = lib.types.str;
        default = defaultUser;
        description = "Group under which the collector runs.";
      };

      environmentFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = ''
          Path to environment file with secrets (NVC_SLSKD_KEY, etc.).
          Available variables:
          - NVC_SLSKD_KEY: slskd API key
          - NVC_TELEGRAM_TOKEN: Telegram bot token (optional)
          - NVC_TELEGRAM_CHAT_IDS: comma-separated chat IDs (optional)
        '';
      };

      settings = lib.mkOption {
        description = "Application configuration.";
        default = { };
        type = lib.types.submodule {
          freeformType = settingsFormat.type;

          options = {
            slskd_url = lib.mkOption {
              type = lib.types.str;
              default = "http://127.0.0.1:5030";
              description = "slskd API base URL.";
            };

            music_dir = lib.mkOption {
              type = lib.types.path;
              default = "/srv/music";
              description = "Navidrome music library directory.";
            };

            download_dir = lib.mkOption {
              type = lib.types.path;
              default = "/var/lib/slskd/downloads";
              description = "slskd download directory.";
            };

            db_path = lib.mkOption {
              type = lib.types.path;
              default = "/var/lib/navidrome-collector/queue.db";
              description = "SQLite queue database path.";
            };
          };
        };
      };
    };
  };

  config = lib.mkIf config.services.navidrome-collector.enable {
    environment.systemPackages = [
      config.services.navidrome-collector.package
    ];

    users.users = lib.optionalAttrs (config.services.navidrome-collector.user == defaultUser) {
      "${defaultUser}" = {
        group = config.services.navidrome-collector.group;
        isSystemUser = true;
      };
    };

    users.groups = lib.optionalAttrs (config.services.navidrome-collector.group == defaultUser) {
      "${defaultUser}" = { };
    };

    systemd.services.navidrome-collector = {
      description = "Navidrome Music Collector — Soulseek-powered downloader";
      after = [ "network.target" "slskd.service" "navidrome.service" ];
      wants = [ "slskd.service" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "oneshot";
        User = config.services.navidrome-collector.user;
        Group = config.services.navidrome-collector.group;
        StateDirectory = "navidrome-collector";
        EnvironmentFile = lib.mkIf (config.services.navidrome-collector.environmentFile != null)
          config.services.navidrome-collector.environmentFile;
        ExecStart = lib.getExe config.services.navidrome-collector.package;
        SupplementaryGroups = [ "slskd" ];
      };

      environment = {
        NVC_SLSKD_URL = config.services.navidrome-collector.settings.slskd_url;
        NVC_MUSIC_DIR = config.services.navidrome-collector.settings.music_dir;
        NVC_DOWNLOAD_DIR = config.services.navidrome-collector.settings.download_dir;
        NVC_DB = config.services.navidrome-collector.settings.db_path;
      };
    };

    # Timer to process queue periodically
    systemd.timers.navidrome-collector = {
      description = "Periodic Navidrome Music Collection";
      partOf = [ "navidrome-collector.service" ];
      wantedBy = [ "timers.target" ];

      timerConfig = {
        OnCalendar = "hourly";
        Persistent = true;
        RandomizedDelaySec = 300;
      };
    };

    # Grant collector access to slskd download dir and navidrome music dir
    systemd.tmpfiles.rules = [
      "d ${config.services.navidrome-collector.settings.db_path} 0750 ${defaultUser} ${defaultUser} - -"
    ];
  };

  meta = {
    maintainers = with lib.maintainers; [ xvantz ];
    description = "NixOS module for Navidrome Music Collector";
  };
}
