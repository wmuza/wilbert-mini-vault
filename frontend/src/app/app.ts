import { HttpClient, HttpEventType } from '@angular/common/http';
import { KeyValuePipe } from '@angular/common';
import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';

// In Docker the FastAPI service serves this bundle, so the API is same-origin
// and a relative URL is correct. Under `ng serve` the app runs on :4200 and the
// API is a separate origin on :8000 (CORS is open on it, so no proxy needed).
const API = location.port === '4200' ? 'http://localhost:8000' : '';

const METRICS_POLL_MS = 5_000;
const HEALTH_POLL_MS = 10_000;

interface VaultFile {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  uploaded_at: string;
  download_count: number;
}

interface FileListResponse {
  source: 'postgres' | 'redis-cache';
  count: number;
  files: VaultFile[];
}

interface Health {
  status: string;
  services: Record<string, string>;
}

interface Metrics {
  generated_at: string;
  files: { count: number; total_bytes: number; avg_bytes: number; largest_bytes: number };
  downloads: {
    total: number;
    pending_flush: number;
    flush_interval_seconds: number;
    top: { id: string; filename: string; downloads: number }[];
  };
  cache: {
    hits: number;
    misses: number;
    hit_rate: number;
    ttl_seconds: number;
    listing_ttl_remaining: number;
  };
  events: { uploads: number; downloads: number; deletes: number };
  by_type: { kind: string; count: number; bytes: number }[];
  uploads_by_day: { day: string; count: number; bytes: number }[];
  services: {
    postgres: { version: string; database_size_bytes: number; file_rows: number };
    redis: {
      version: string;
      used_memory_human: string;
      connected_clients: number;
      uptime_seconds: number;
      keyspace_hits: number;
      keyspace_misses: number;
      keys: number;
    };
    minio: { bucket: string; objects: number; bytes: number; endpoint: string };
  };
}

interface Toast {
  id: number;
  text: string;
  kind: 'ok' | 'err';
}

interface Tip {
  x: number;
  y: number;
  label: string;
  value: string;
}

/** Axis ticks on round numbers (0 / 2 / 4), never 0 / 1.7 / 3.4. */
function niceTicks(max: number, count = 3): number[] {
  if (max <= 0) return [0, 1];
  const raw = max / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)) || 0);
  const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? 10 * magnitude;
  const top = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let v = 0; v <= top + step / 1000; v += step) ticks.push(Math.round(v * 1000) / 1000);
  return ticks;
}

/**
 * A bar whose top corners are rounded and whose base is square, so it reads as
 * growing out of the baseline rather than floating above it.
 */
function columnPath(x: number, y: number, w: number, h: number, r = 4): string {
  if (h <= 0.5) return '';
  const radius = Math.min(r, h, w / 2);
  const bottom = y + h;
  return [
    `M${x},${bottom}`,
    `L${x},${y + radius}`,
    `Q${x},${y} ${x + radius},${y}`,
    `L${x + w - radius},${y}`,
    `Q${x + w},${y} ${x + w},${y + radius}`,
    `L${x + w},${bottom}`,
    'Z',
  ].join(' ');
}

@Component({
  selector: 'app-root',
  imports: [KeyValuePipe],
  templateUrl: './app.html',
})
export class App implements OnInit, OnDestroy {
  private readonly http = inject(HttpClient);
  readonly API = API;

  readonly health = signal<Health | null>(null);
  readonly metrics = signal<Metrics | null>(null);
  readonly files = signal<VaultFile[]>([]);
  readonly source = signal<'postgres' | 'redis-cache' | ''>('');
  readonly busy = signal(false);
  readonly progress = signal(0);
  readonly uploadingName = signal('');
  readonly dragOver = signal(false);
  readonly toasts = signal<Toast[]>([]);
  readonly confirmingId = signal<string | null>(null);
  readonly showTables = signal(false);
  readonly tip = signal<Tip | null>(null);

  readonly totalBytes = computed(() =>
    this.files().reduce((sum, f) => sum + f.size_bytes, 0),
  );

  /** Postgres rows vs MinIO objects, counted independently. Drift is a bug worth seeing. */
  readonly storageDrift = computed(() => {
    const m = this.metrics();
    if (!m) return 0;
    return m.services.minio.objects - m.services.postgres.file_rows;
  });

  // ── Chart geometry ────────────────────────────────────────────────────
  // Single series everywhere, so one hue and no legend: the card title says
  // what is plotted. Values live on the axis, the extreme is direct-labelled,
  // and the rest are reachable by hover, focus, or the table view.

  readonly uploadsChart = computed(() => {
    const days = this.metrics()?.uploads_by_day ?? [];
    const W = 640;
    const H = 190;
    const padL = 34;
    const padR = 10;
    const padT = 16;
    const padB = 24;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const peak = Math.max(0, ...days.map((d) => d.count));
    const ticks = niceTicks(peak);
    const top = Math.max(ticks[ticks.length - 1], 1);
    const band = plotW / Math.max(days.length, 1);
    const barW = Math.max(3, Math.min(24, band - 8));

    const bars = days.map((d, i) => {
      const h = (d.count / top) * plotH;
      const x = padL + i * band + (band - barW) / 2;
      const y = padT + plotH - h;
      // A 3px-tall bar still needs a hit target you can actually land on.
      const hitY = Math.min(y, padT + plotH - 24);
      return {
        ...d,
        x,
        y,
        w: barW,
        h,
        hitY,
        hitH: padT + plotH - hitY,
        cx: x + barW / 2,
        path: columnPath(x, y, barW, h),
        tick: this.dayTick(d.day),
        // Only the peak is direct-labelled; a number on every column is noise.
        peak: peak > 0 && d.count === peak,
        // Thin out the axis ticks, and keep the last two apart: the final day
        // is always labelled, so a regular tick right beside it would collide.
        showTick: i === days.length - 1 || (i % 3 === 0 && i < days.length - 2),
      };
    });

    return {
      W,
      H,
      padL,
      padT,
      padB,
      plotH,
      plotW,
      bars,
      grid: ticks.map((t) => ({ y: padT + plotH - (t / top) * plotH, value: t })),
      total: days.reduce((s, d) => s + d.count, 0),
      empty: peak === 0,
    };
  });

  /** Horizontal bars are plain HTML: real text, no viewBox scaling, crisp at any width. */
  readonly typeBars = computed(() => {
    const rows = this.metrics()?.by_type ?? [];
    const peak = Math.max(1, ...rows.map((r) => r.bytes));
    return rows.map((r) => ({ ...r, pct: (r.bytes / peak) * 100 }));
  });

  readonly topBars = computed(() => {
    const rows = (this.metrics()?.downloads.top ?? []).filter((r) => r.downloads > 0);
    const peak = Math.max(1, ...rows.map((r) => r.downloads));
    return rows.map((r) => ({ ...r, pct: (r.downloads / peak) * 100 }));
  });

  readonly hitRatePct = computed(() => Math.round((this.metrics()?.cache.hit_rate ?? 0) * 100));

  private toastSeq = 0;
  private confirmTimer: ReturnType<typeof setTimeout> | null = null;
  private timers: ReturnType<typeof setInterval>[] = [];

  ngOnInit(): void {
    this.loadHealth();
    this.loadFiles();
    this.loadMetrics();
    this.timers.push(setInterval(() => this.loadHealth(), HEALTH_POLL_MS));
    this.timers.push(setInterval(() => this.loadMetrics(), METRICS_POLL_MS));
  }

  ngOnDestroy(): void {
    this.timers.forEach(clearInterval);
    if (this.confirmTimer) clearTimeout(this.confirmTimer);
  }

  loadHealth(): void {
    this.http.get<Health>(`${API}/health`).subscribe({
      next: (h) => this.health.set(h),
      // A degraded stack answers 503, which lands here with the same body.
      error: (err) =>
        this.health.set(
          err?.error?.services ? err.error : { status: 'unreachable', services: {} },
        ),
    });
  }

  loadMetrics(): void {
    // No skeleton on refetch: the previous render simply stays until this lands.
    this.http.get<Metrics>(`${API}/metrics`).subscribe({
      next: (m) => this.metrics.set(m),
      error: () => {},
    });
  }

  loadFiles(): void {
    this.http.get<FileListResponse>(`${API}/files`).subscribe({
      next: (res) => {
        // Counts arrive merged into the listing — one request, not one per file.
        this.files.set(res.files);
        this.source.set(res.source);
      },
      error: () => this.toast('Could not reach the API.', 'err'),
    });
  }

  private refresh(): void {
    this.loadFiles();
    this.loadMetrics();
  }

  // ── Upload ──────────────────────────────────────────────────────────

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    this.dragOver.set(true);
  }

  onDrop(event: DragEvent): void {
    event.preventDefault();
    this.dragOver.set(false);
    const file = event.dataTransfer?.files?.[0];
    if (file) this.upload(file);
  }

  onPick(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (file) this.upload(file);
    input.value = ''; // allow re-picking the same file
  }

  private upload(file: File): void {
    this.busy.set(true);
    this.progress.set(0);
    this.uploadingName.set(file.name);

    const form = new FormData();
    form.append('file', file);

    this.http
      .post(`${API}/files`, form, { reportProgress: true, observe: 'events' })
      .subscribe({
        next: (event) => {
          if (event.type === HttpEventType.UploadProgress && event.total) {
            this.progress.set(Math.round((100 * event.loaded) / event.total));
          }
          if (event.type === HttpEventType.Response) {
            this.busy.set(false);
            this.toast(`Stored ${file.name} — bytes in MinIO, row in Postgres, cache invalidated.`);
            this.refresh();
          }
        },
        error: (err) => {
          this.busy.set(false);
          this.toast(`Upload failed (${err.status || 'network error'}).`, 'err');
        },
      });
  }

  // ── Download / delete ───────────────────────────────────────────────

  onDownload(): void {
    // The browser follows the href; the API counts the download only once the
    // bytes have finished streaming, so give it a beat before re-reading.
    setTimeout(() => this.refresh(), 900);
  }

  askRemove(f: VaultFile): void {
    this.confirmingId.set(f.id);
    if (this.confirmTimer) clearTimeout(this.confirmTimer);
    this.confirmTimer = setTimeout(() => this.confirmingId.set(null), 3000);
  }

  remove(f: VaultFile): void {
    this.confirmingId.set(null);
    this.http.delete(`${API}/files/${f.id}`).subscribe({
      next: () => {
        this.toast(`Deleted ${f.filename} from MinIO and Postgres.`);
        this.refresh();
      },
      error: (err) => this.toast(`Delete failed (${err.status}).`, 'err'),
    });
  }

  // ── Hover / focus tooltip ───────────────────────────────────────────
  // Keyboard focus shows exactly what hover shows, so the values are never
  // gated behind a pointer.

  showTip(event: MouseEvent | FocusEvent, label: string, value: string): void {
    const target = event.currentTarget as SVGElement;
    const card = target.closest('.chart-card') as HTMLElement | null;
    if (!card) return;
    const box = card.getBoundingClientRect();
    const mark = target.getBoundingClientRect();
    this.tip.set({
      x: mark.left - box.left + mark.width / 2,
      y: mark.top - box.top,
      label,
      value,
    });
  }

  hideTip(): void {
    this.tip.set(null);
  }

  // ── Presentation helpers ────────────────────────────────────────────

  private toast(text: string, kind: 'ok' | 'err' = 'ok'): void {
    const id = ++this.toastSeq;
    this.toasts.update((list) => [...list, { id, text, kind }]);
    setTimeout(
      () => this.toasts.update((list) => list.filter((t) => t.id !== id)),
      4000,
    );
  }

  kindOf(f: VaultFile): string {
    const t = f.content_type;
    const name = f.filename.toLowerCase();
    if (t.startsWith('image/')) return 'IMG';
    if (t.startsWith('video/')) return 'VID';
    if (t.startsWith('audio/')) return 'AUD';
    if (t === 'application/pdf') return 'PDF';
    if (/zip|tar|gzip|7z|compressed/.test(t) || /\.(zip|tar|gz|7z|rar)$/.test(name)) return 'ZIP';
    if (/json|javascript|xml|html|css/.test(t) || /\.(ts|py|js|json|yml|yaml|sh|sql)$/.test(name)) return 'CODE';
    if (t.startsWith('text/')) return 'TXT';
    return 'BIN';
  }

  formatBytes(n: number): string {
    if (n < 1024) return `${n} B`;
    if (n < 1_048_576) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1_073_741_824) return `${(n / 1_048_576).toFixed(1)} MB`;
    return `${(n / 1_073_741_824).toFixed(2)} GB`;
  }

  /** Compact figures for stat tiles: 1,284 / 12.9K / 3.4M. */
  formatNum(n: number): string {
    if (n < 1000) return `${n}`;
    if (n < 10_000) return n.toLocaleString();
    if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`;
    return `${(n / 1_000_000).toFixed(1)}M`;
  }

  formatDuration(seconds: number): string {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86_400) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86_400)}d`;
  }

  /** "Jul 9" for an axis tick. */
  dayTick(iso: string): string {
    const d = new Date(`${iso}T00:00:00`);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  relativeTime(iso: string): string {
    const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 45) return 'just now';
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 86_400) return `${Math.round(seconds / 3600)} h ago`;
    return new Date(iso).toLocaleDateString();
  }
}
