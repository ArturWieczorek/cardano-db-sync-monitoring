#!/usr/bin/env python3
import argparse
import os
import sqlite3
import time
from datetime import datetime
from threading import Thread
from typing import Any

import pandas as pd
import plotly.graph_objs as go
import psutil
import psycopg2
from plotly.graph_objs import Figure
from plotly.subplots import make_subplots
from psutil import Process


class CardanoMonitor:
    def __init__(self, env: str, db_sync_ver: str, pg_host: str, pg_port: str, pg_user: str, pg_dbname: str) -> None:
        self.running: bool = True
        self.env: str = env
        self.db_sync_ver: str = db_sync_ver
        self.pg_host: str = pg_host
        self.pg_port: str = pg_port
        self.pg_user: str = pg_user
        self.pg_dbname: str = pg_dbname

        self.db_file: str = f"dbsync_{self.env}_stats_sqlite.db"
        self.output_folder: str = 'plots'
        os.makedirs(self.output_folder, exist_ok=True)

        self.init_db()

    def init_db(self) -> None:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS memory_metrics
                         (slot_no INTEGER, rss REAL, vms REAL, uss REAL,
                          pss REAL, swap REAL, shared REAL, version TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS cpu_metrics
                         (slot_no INTEGER, cpu_percent REAL, user_time REAL,
                          system_time REAL, children_user REAL, children_system REAL,
                          iowait REAL, ctx_switches INTEGER, interrupts INTEGER,
                          version TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS db_sync_version
                         (timestamp TEXT, version TEXT)''')
            conn.commit()

    def get_process(self) -> Process | None:
        for proc in psutil.process_iter(['name', 'cmdline']):
            if 'cardano-db-sync' in ' '.join(proc.info['cmdline'] or []):
                return proc
        return None

    def get_memory_details(self, process: Process) -> dict[str, float] | None:
        try:
            mi = process.memory_info()
            mfi = process.memory_full_info()
            return {
                'rss': mi.rss / 1024**2,
                'vms': mi.vms / 1024**2,
                'uss': getattr(mfi, 'uss', 0) / 1024**2,
                'pss': getattr(mfi, 'pss', 0) / 1024**2,
                'swap': getattr(mfi, 'swap', 0) / 1024**2,
                'shared': getattr(mi, 'shared', 0) / 1024**2
            }
        except Exception:
            return None

    def get_cpu_details(self, process: Process) -> dict[str, Any] | None:
        try:
            times = process.cpu_times()
            percent = process.cpu_percent(interval=None)
            with process.oneshot():
                ctx = process.num_ctx_switches()
            return {
                'cpu_percent': percent,
                'user_time': times.user,
                'system_time': times.system,
                'children_user': getattr(times, 'children_user', 0.0),
                'children_system': getattr(times, 'children_system', 0.0),
                'iowait': getattr(times, 'iowait', 0.0),
                'ctx_switches': ctx.voluntary + ctx.involuntary,
                'interrupts': None
            }
        except Exception:
            return None

    def get_slot_no(self) -> int | None:
        try:
            conn = psycopg2.connect(
                host=self.pg_host, port=self.pg_port,
                user=self.pg_user, dbname=self.pg_dbname
            )
            cur = conn.cursor()
            cur.execute("SELECT slot_no FROM block WHERE block_no IS NOT NULL ORDER BY block_no DESC LIMIT 1;")
            r = cur.fetchone()
            conn.close()
            return r[0] if r else None
        except Exception as e:
            print("Postgres error:", e)
            return None

    def get_sync_percent(self) -> float | None:
        sql = """
          SELECT
            100 * (
              EXTRACT(EPOCH FROM (MAX(time) AT TIME ZONE 'UTC'))
              - EXTRACT(EPOCH FROM (MIN(time) AT TIME ZONE 'UTC'))
            )
            / (
              EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'UTC'))
              - EXTRACT(EPOCH FROM (MIN(time) AT TIME ZONE 'UTC'))
            )
            AS sync_percent
          FROM block;
        """
        try:
            conn = psycopg2.connect(
                host=self.pg_host,
                port=self.pg_port,
                user=self.pg_user,
                dbname=self.pg_dbname,
            )
            cur = conn.cursor()
            cur.execute(sql)
            row = cur.fetchone()
            conn.close()
            return float(row[0]) if row and row[0] is not None else None
        except Exception as e:
            print(f"Error fetching sync percent: {e}")
            return None

    def get_db_sync_version(self) -> str:
        return f"cardano-db-sync {self.db_sync_ver} {self.env}"

    def log_metrics(self) -> None:
        proc = self.get_process()
        if proc:
            proc.cpu_percent(interval=None)
        while self.running:
            slot = self.get_slot_no()
            if slot is None:
                time.sleep(10)
                continue

            proc = self.get_process()
            mem = self.get_memory_details(proc) if proc else None
            cpu = self.get_cpu_details(proc) if proc else None
            ver = self.get_db_sync_version()
            sync_progress = self.get_sync_percent()

            if mem:
                with sqlite3.connect(self.db_file) as conn:
                    conn.execute(
                        "INSERT INTO memory_metrics VALUES (?,?,?,?,?,?,?,?)",
                        (slot, mem['rss'], mem['vms'], mem['uss'],
                         mem['pss'], mem['swap'], mem['shared'], ver)
                    )
            if cpu:
                with sqlite3.connect(self.db_file) as conn:
                    conn.execute(
                        "INSERT INTO cpu_metrics VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (slot, cpu['cpu_percent'], cpu['user_time'], cpu['system_time'],
                         cpu['children_user'], cpu['children_system'],
                         cpu['iowait'], cpu['ctx_switches'], cpu['interrupts'], ver)
                    )
            with sqlite3.connect(self.db_file) as conn:
                conn.execute(
                    "INSERT INTO db_sync_version VALUES (?,?)",
                    (datetime.now().isoformat(), ver)
                )

            print(f"Slot {slot} | Sync Progress: {sync_progress:.2f}% | "
                  f"CPU {cpu['cpu_percent'] if cpu else 'N/A'}% | RSS {mem['rss'] if mem else 'N/A'}MB")
            time.sleep(10)

    def save_plot(self, fig: Figure, versions: list[str]) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = os.path.join(self.output_folder, f"comparison_{self.pg_dbname}_{ts}.html")
        fig.write_html(fn)
        print("Saved:", fn)

    def plot_metrics(self, versions: list[str]) -> None:
        placeholders = ",".join("?" * len(versions))
        qm = f"SELECT slot_no, rss, version FROM memory_metrics WHERE version IN ({placeholders}) ORDER BY slot_no"
        qc = f"SELECT slot_no, cpu_percent, version FROM cpu_metrics WHERE version IN ({placeholders}) ORDER BY slot_no"

        with sqlite3.connect(self.db_file) as conn:
            mem_df = pd.read_sql_query(qm, conn, params=versions)
            cpu_df = pd.read_sql_query(qc, conn, params=versions)

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            subplot_titles=["Memory (RSS)", "CPU (%)"])
        for v in versions:
            dfm = mem_df[mem_df.version == v]
            fig.add_trace(go.Scatter(x=dfm.slot_no, y=dfm.rss,
                                     mode='lines', name=f"Mem-{v}"),
                          row=1, col=1)
            dfc = cpu_df[cpu_df.version == v]
            fig.add_trace(go.Scatter(x=dfc.slot_no, y=dfc.cpu_percent,
                                     mode='lines', name=f"CPU-{v}"),
                          row=2, col=1)

        fig.update_layout(
            title=f"{self.env} {self.db_sync_ver} Metrics",
            xaxis_title="Slot Number", yaxis_title="RSS (MB)",
            xaxis2_title="Slot Number", yaxis2_title="CPU Usage (%)"
        )
        fig.show()
        self.save_plot(fig, versions)

    def run(self) -> None:
        t = Thread(target=self.log_metrics, daemon=True)
        t.start()
        try:
            while True:
                with sqlite3.connect(self.db_file) as conn:
                    vers_df = pd.read_sql_query(
                        "SELECT DISTINCT version FROM db_sync_version ORDER BY timestamp DESC",
                        conn
                    )
                vers = vers_df["version"].tolist()

                if not vers:
                    print("waiting for versions…")
                    time.sleep(10)
                    continue

                for i, v in enumerate(vers, 1):
                    print(f"{i}. {v}")
                sel = input("choose (e.g. 1,2): ")
                chosen = [vers[int(i) - 1] for i in sel.split(",")]
                self.plot_metrics(chosen)
                time.sleep(60)
        except KeyboardInterrupt:
            self.running = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cardano DB-Sync resources monitor")
    parser.add_argument("--env",
                        required=True,
                        help="Environment name (e.g. preview, preprod, mainnet)")
    parser.add_argument("--db-sync-ver",
                        required=True,
                        help="DB-Sync version (e.g. 13.6.0.5)")
    parser.add_argument("--pg-host",
                        default="localhost",
                        help="Postgres host")
    parser.add_argument("--pg-port",
                        default="5432",
                        help="Postgres port")
    parser.add_argument("--pg-user",
                        default="postgres",
                        help="Postgres user")
    parser.add_argument("--pg-dbname",
                        help="Postgres database name (defaults to <env>_<db-sync-ver>_metrics)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    pg_dbname = args.pg_dbname or f"{args.env}_{args.db_sync_ver}_metrics"

    monitor = CardanoMonitor(
        env=args.env,
        db_sync_ver=args.db_sync_ver,
        pg_host=args.pg_host,
        pg_port=args.pg_port,
        pg_user=args.pg_user,
        pg_dbname=pg_dbname
    )
    monitor.run()

