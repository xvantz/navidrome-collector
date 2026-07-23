"""CLI interface for navidrome-collector."""

import logging
import sys
from pathlib import Path

import click

from . import __version__
from .queue import Queue
from .slskd_client import SlskdClient

log = logging.getLogger(__name__)
from .collector import Collector

_DEFAULT_CONFIG = Path("/etc/navidrome-collector/config.yaml")


@click.group()
@click.version_option(version=__version__)
@click.option("--db", default="/var/lib/navidrome-collector/queue.db", envvar="NVC_DB",
              help="Path to SQLite queue database")
@click.option("--slskd-url", default="http://127.0.0.1:5030", envvar="NVC_SLSKD_URL",
              help="slskd API base URL")
@click.option("--slskd-key", default=None, envvar="NVC_SLSKD_KEY",
              help="slskd API key")
@click.option("--music-dir", default="/srv/music", envvar="NVC_MUSIC_DIR",
              help="Navidrome music directory")
@click.option("--download-dir", default="/var/lib/slskd/downloads", envvar="NVC_DOWNLOAD_DIR",
              help="slskd download directory")
@click.option("--ytdlp-dir", default=None, envvar="NVC_YTDIR",
              help="yt-dlp download directory (default: download-dir/ytdlp)")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx, db, slskd_url, slskd_key, music_dir, download_dir, ytdlp_dir, verbose):
    """Navidrome Music Collector — Soulseek-powered music downloader."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    ctx.ensure_object(dict)
    ctx.obj["queue"] = Queue(db)
    ctx.obj["slskd"] = SlskdClient(slskd_url, api_key=slskd_key)
    ctx.obj["music_dir"] = Path(music_dir)
    ctx.obj["download_dir"] = Path(download_dir)
    ctx.obj["ytdlp_dir"] = Path(ytdlp_dir) if ytdlp_dir else None


# ── Queue commands ───────────────────────────────────────

@cli.group()
def queue():
    """Manage the download queue."""


@queue.command("add")
@click.argument("query")
@click.option("--artist", "-a", help="Artist name (optional)")
@click.option("--title", "-t", help="Track title (optional)")
@click.pass_context
def queue_add(ctx, query, artist, title):
    """Add a track to the download queue."""
    q: Queue = ctx.obj["queue"]
    item_id = q.add(query, artist=artist, title=title)
    click.echo(f"Added #{item_id}: {query}")


@queue.command("list")
@click.option("--status", "-s", default=None, help="Filter by status")
@click.pass_context
def queue_list(ctx, status):
    """List queue items."""
    q: Queue = ctx.obj["queue"]
    items = q.list_items(status=status)
    if not items:
        click.echo("Queue is empty.")
        return

    click.echo(f"{'ID':>4}  {'Status':<12}  {'Query'}")
    click.echo("-" * 60)
    for item in items:
        click.echo(f"{item.id:>4}  {item.status:<12}  {item.query}")
        if item.error:
            click.echo(f"      error: {item.error}")


@queue.command("stats")
@click.pass_context
def queue_stats(ctx):
    """Show queue statistics."""
    q: Queue = ctx.obj["queue"]
    stats = q.stats()
    if not stats:
        click.echo("Queue is empty.")
        return
    total = sum(stats.values())
    click.echo(f"Total: {total}")
    for status, count in sorted(stats.items()):
        click.echo(f"  {status}: {count}")


@queue.command("clear")
@click.option("--status", "-s", default=None, help="Clear only items with this status")
@click.pass_context
def queue_clear(ctx, status):
    """Clear the queue."""
    q: Queue = ctx.obj["queue"]
    count = q.clear(status=status)
    click.echo(f"Cleared {count} items.")


# ── Process commands ──────────────────────────────────────

@cli.command()
@click.option("--max-items", "-n", default=0, type=int,
              help="Max items to process (0 = unlimited)")
@click.pass_context
def process(ctx, max_items):
    """Process pending items in the queue."""
    slskd: SlskdClient = ctx.obj["slskd"]
    if not slskd.ping():
        click.echo("slskd is not reachable. Is it running?", err=True)
        sys.exit(1)

    collector = Collector(
        queue=ctx.obj["queue"],
        slskd=slskd,
        music_dir=ctx.obj["music_dir"],
        download_dir=ctx.obj["download_dir"],
        ytdlp_dir=ctx.obj["ytdlp_dir"],
    )
    stats = collector.process_queue(max_items=max_items)
    click.echo(
        f"Done: {stats['processed']} processed, "
        f"{stats['succeeded']} succeeded, "
        f"{stats['failed']} failed."
    )


# ── Daemon ─────────────────────────────────────────────

@cli.command()
@click.option("--interval", "-i", default=30, type=int,
              help="Polling interval in seconds")
@click.option("--once", is_flag=True,
              help="Process queue once and exit (same as `process`)")
@click.pass_context
def daemon(ctx, interval, once):
    """Run continuously, monitoring and processing the queue."""
    import time
    from .collector import Collector
    from .notifier import send_message

    slskd = ctx.obj["slskd"]
    if not slskd.ping():
        click.echo("slskd is not reachable.", err=True)
        raise SystemExit(1)

    collector = Collector(
        queue=ctx.obj["queue"],
        slskd=slskd,
        music_dir=ctx.obj["music_dir"],
        download_dir=ctx.obj["download_dir"],
        ytdlp_dir=ctx.obj["ytdlp_dir"],
    )

    send_message("🎵 Navidrome Collector started")

    if once:
        stats = collector.process_queue()
        click.echo(f"Done: {stats['succeeded']} ok, {stats['failed']} failed")
        return

    click.echo(f"Daemon mode: polling every {interval}s (Ctrl+C to stop)")
    while True:
        try:
            stats = collector.process_queue()
            if stats["succeeded"]:
                send_message(f"✅ Downloaded {stats['succeeded']} track(s)")
            if stats["failed"]:
                send_message(f"❌ {stats['failed']} download(s) failed")
            # Check and report active downloads
            downloads = slskd.get_downloads()
            active = [d for d in downloads if "Queued" in d.state or "InProgress" in d.state or "Requested" in d.state]
            if active:
                for d in active[:3]:
                    name = d.filename.split("\\")[-1].split("/")[-1][:40]
                    pct = f"{d.bytes_downloaded}/{d.size}KB" if d.size else "waiting"
                    log.info("  %s: %s — %s", d.username, name, pct)

            # Listen for Telegram commands
            try:
                from .notifier import listen_and_handle
                handled = listen_and_handle(
                    lambda q: collector.queue.add(q),
                    lambda: collector.queue.list_items(),
                )
                if handled:
                    log.info("Handled %d Telegram command(s)", handled)
            except Exception:
                pass

        except Exception as e:
            log.exception("Daemon error: %s", e)

        time.sleep(interval)


# ── Info ──────────────────────────────────────────────────

@cli.command()
@click.pass_context
def check(ctx):
    """Check connectivity to slskd and query queue status."""
    slskd: SlskdClient = ctx.obj["slskd"]
    ok = slskd.ping()
    click.echo(f"slskd: {'✅ reachable' if ok else '❌ unreachable'}")

    q: Queue = ctx.obj["queue"]
    stats = q.stats()
    click.echo(f"Queue: {sum(stats.values())} total" if stats else "Queue: empty")

    if ok:
        downloads = slskd.get_downloads()
        active = [d for d in downloads if d.state in ("Queued", "InProgress")]
        click.echo(f"Active downloads: {len(active)}")


def main():
    cli(auto_envvar_prefix="NVC")


if __name__ == "__main__":
    main()
