#!/usr/bin/env python3
import sqlite3
import time
from datetime import datetime
from threading import Thread

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import psutil
from psutil import Process


class CardanoMonitor:
    def __init__(self) -> None:
        self.running: bool = True
        self.db_file: str = 'simple_monitoring_sqlite.db'
        self.init_db()

    def init_db(self) -> None:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            # Create tables if they don't exist
            c.execute('''CREATE TABLE IF NOT EXISTS memory_metrics
                         (timestamp TEXT, rss REAL, vms REAL, uss REAL, 
                          pss REAL, swap REAL, shared REAL, process TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS cpu_metrics
                         (timestamp TEXT, cpu_percent REAL, user_time REAL,
                          system_time REAL, children_user REAL, children_system REAL,
                          iowait REAL, ctx_switches INTEGER, interrupts INTEGER,
                          process TEXT)''')
            conn.commit()

    def get_process(self) -> Process | None:
        for proc in psutil.process_iter(['name', 'cmdline']):
            if 'cardano-db-sync' in ' '.join(proc.info.get('cmdline') or []):
                return proc
        return None

    def get_memory_details(self, process: Process) -> dict[str, float | None] | None:
        try:
            mem_info = process.memory_info()
            mem_full = process.memory_full_info()
            return {
                'rss': mem_info.rss / 1024 / 1024,  # MB
                'vms': mem_info.vms / 1024 / 1024,
                'uss': mem_full.uss / 1024 / 1024 if hasattr(mem_full, 'uss') else None,
                'pss': mem_full.pss / 1024 / 1024 if hasattr(mem_full, 'pss') else None,
                'swap': mem_full.swap / 1024 / 1024 if hasattr(mem_full, 'swap') else None,
                'shared': mem_info.shared / 1024 / 1024
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            return None

    def get_cpu_details(self, process: Process) -> dict[str, float | None] | None:
        try:
            cpu_times = process.cpu_times()
            cpu_percent = process.cpu_percent(interval=None)  # Use pre-primed counter
            cpu_count = psutil.cpu_count() or 1  # Avoid division by zero

            with process.oneshot():
                num_ctx_switches = process.num_ctx_switches()

            return {
                'cpu_percent': cpu_percent,
                'cpu_percent_normalized': cpu_percent / cpu_count,
                'user_time': cpu_times.user,
                'system_time': cpu_times.system,
                'children_user': getattr(cpu_times, 'children_user', 0.0),
                'children_system': getattr(cpu_times, 'children_system', 0.0),
                'iowait': getattr(cpu_times, 'iowait', None),
                'ctx_switches': num_ctx_switches.voluntary + num_ctx_switches.involuntary,
                'interrupts': None  # or remove this field entirely
            }

        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError) as e:
            print(f"Error collecting CPU data: {e}")
            return None

    def log_metrics(self) -> None:
        proc = self.get_process()
        if proc:
            proc.cpu_percent(interval=None)  # Prime it once before the loop

        while self.running:
            timestamp = datetime.now().isoformat()
            proc = self.get_process()

            if proc:
                # Memory metrics
                mem_data = self.get_memory_details(proc)

                # CPU metrics
                cpu_data = self.get_cpu_details(proc)

                if mem_data:
                    with sqlite3.connect(self.db_file) as conn:
                        c = conn.cursor()
                        c.execute('''INSERT INTO memory_metrics 
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                                  (timestamp, mem_data['rss'], mem_data['vms'],
                                   mem_data.get('uss'), mem_data.get('pss'),
                                   mem_data.get('swap'), mem_data['shared'],
                                   'cardano-db-sync'))
                        conn.commit()

                if cpu_data:
                    with sqlite3.connect(self.db_file) as conn:
                        c = conn.cursor()
                        c.execute('''INSERT INTO cpu_metrics 
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                  (timestamp, cpu_data['cpu_percent'],
                                   cpu_data['user_time'], cpu_data['system_time'],
                                   cpu_data['children_user'], cpu_data['children_system'],
                                   cpu_data['iowait'], cpu_data['ctx_switches'],
                                   cpu_data['interrupts'], 'cardano-db-sync'))
                        conn.commit()

                print(f"{timestamp} - CPU: {cpu_data['cpu_percent'] if cpu_data else 'N/A'}% | "
                      f"RSS: {mem_data['rss'] if mem_data else 'N/A'}MB")
            time.sleep(10)

    def plot_metrics(self, hours: int = 24) -> None:
        with sqlite3.connect(self.db_file) as conn:
            # Memory data
            mem_df = pd.read_sql_query(
                f"""SELECT timestamp, rss, vms, uss, pss, swap, shared 
                    FROM memory_metrics 
                    WHERE datetime(timestamp) >= datetime('now', '-{hours} hours')""",
                conn
            )

            # CPU data
            cpu_df = pd.read_sql_query(
                f"""SELECT timestamp, cpu_percent, user_time, system_time, iowait,
                           ctx_switches, interrupts
                    FROM cpu_metrics 
                    WHERE datetime(timestamp) >= datetime('now', '-{hours} hours')""",
                conn
            )

        if mem_df.empty or cpu_df.empty:
            print("Not enough data to visualize")
            return

        # Convert timestamps
        for df in [mem_df, cpu_df]:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)

        # Create figure with subplots
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 12))

        # Memory plot
        ax1.plot(mem_df.index, mem_df['rss'], label='RSS', color='blue')
        if 'uss' in mem_df and not mem_df['uss'].isnull().all():
            ax1.plot(mem_df.index, mem_df['uss'], label='USS', color='green')
        ax1.set_ylabel('Memory (MB)')
        ax1.set_title(f'cardano-db-sync Resource Usage (Last {hours} hours)')
        ax1.legend()
        ax1.grid(True)

        # CPU percentage plot
        ax2.plot(cpu_df.index, cpu_df['cpu_percent'], label='CPU %', color='red')
        if 'iowait' in cpu_df and not cpu_df['iowait'].isnull().all():
            ax2.plot(cpu_df.index, cpu_df['iowait'], label='I/O Wait', color='orange')
        ax2.set_ylabel('CPU Usage (%)')
        ax2.legend()
        ax2.grid(True)

        # Format x-axes
        for ax in [ax1, ax2]:
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=max(1, hours // 6)))

        plt.tight_layout()
        plt.savefig('cardano_db_sync_metrics_from_simple_monitoring.png')
        plt.show()

    def run(self) -> None:
        monitor_thread = Thread(target=self.log_metrics)
        monitor_thread.daemon = True
        monitor_thread.start()

        try:
            while True:
                self.plot_metrics()
                time.sleep(60)  # Update plot every minute
        except KeyboardInterrupt:
            self.running = False
            print("Monitoring stopped")


if __name__ == "__main__":
    monitor = CardanoMonitor()
    monitor.run()

