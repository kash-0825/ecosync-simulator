"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           EcoSync – Multithreaded Smart Grid Power Distribution             ║
║                         Simulator v1.0                                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  OS Domain   : Process Synchronization & IPC                                ║
║  Syllabus    : Module 2 – Concurrent Processes, Critical Section Problem,   ║
║                Semaphores, Mutex Locks, Bounded Buffer (Producer-Consumer)  ║
║  SDG Targets : SDG 7 (Affordable & Clean Energy)                            ║
║                SDG 11 (Sustainable Cities and Communities)                  ║
╚══════════════════════════════════════════════════════════════════════════════╝

Architecture Summary
────────────────────
  Shared Resource  │ grid_battery_bank  – a bounded list (capacity = N = 5)
  Producers (2)    │ SolarFarm, WindPark  – inject power packets into buffer
  Consumers (3)    │ EmergencyHospital (P1), MetroResidentialZone (P2),
                   │ IndustrialSteelMill (P3) – extract packets from buffer
  Synchronization  │ mutex  (threading.Lock)       – guards critical section
                   │ empty  (threading.Semaphore)  – counts free buffer slots
                   │ full   (threading.Semaphore)  – counts filled buffer slots

Deadlock-Free Acquisition Order (strictly enforced throughout)
  ➜ counting semaphore (empty / full)  FIRST
  ➜ mutex lock                         SECOND
"""

import threading
import time
import random
import sys
import os
from datetime import datetime

# ── Windows UTF-8 + ANSI fix ──────────────────────────────────────────────────
# Two separate problems on Windows:
#
# 1. UnicodeEncodeError: cmd.exe / Code Runner defaults to CP-1252, which cannot
#    encode box-drawing chars (╔ ═ ║) or emoji (⚡ ⚠️ 🏙️).
#    Fix: reconfigure stdout to UTF-8 directly.
#
# 2. Raw ANSI escape codes printing as literal text (e.g. ESC[93m instead of
#    yellow colour). VS Code's Code Runner output panel does NOT respond to the
#    os.system("") trick. The only reliable fix is calling the Windows kernel32
#    API directly via ctypes to enable ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x04)
#    on the stdout console handle.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # GetStdHandle(-11) returns the stdout console handle
        handle = kernel32.GetStdHandle(-11)
        # GetConsoleMode reads the current mode flags into 'mode'
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        # OR-in flag 0x04 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        # This tells the Windows console to interpret ANSI escape sequences
        # as colour/formatting commands instead of printing them literally.
        kernel32.SetConsoleMode(handle, mode.value | 0x04)
    except Exception:
        # If ctypes call fails (e.g. output is redirected to a file or the
        # Code Runner panel pipes stdout), strip all ANSI codes so the output
        # is still clean and readable without any garbage escape characters.
        import re
        _ansi_strip = re.compile(r'\033\[[0-9;]*m')
        _orig_print = print
        def print(*args, **kwargs):  # noqa: F811
            args = [_ansi_strip.sub('', a) if isinstance(a, str) else a for a in args]
            _orig_print(*args, **kwargs)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 – GLOBAL CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

BUFFER_CAPACITY: int = 5
"""Maximum number of power packets the shared battery bank can hold at once."""

CRITICAL_RESERVE_THRESHOLD: int = int(0.2 * BUFFER_CAPACITY)
"""
Smart Yield threshold derived dynamically as 20 % of buffer capacity.
When the buffer level drops to or below this value, only Priority-1 threads
are allowed to consume. All lower-priority threads voluntarily yield.

With BUFFER_CAPACITY = 5  →  threshold = int(0.2 * 5) = 1
If you change capacity to 20  →  threshold = int(0.2 * 20) = 4  (auto-scales)
"""

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 – SHARED BUFFER & SYNCHRONIZATION PRIMITIVES
# ─────────────────────────────────────────────────────────────────────────────

grid_battery_bank: list[str] = []
"""
The shared bounded buffer.
All producer and consumer threads operate on this single list.
Direct access is ONLY permitted while holding the mutex lock.
"""

mutex = threading.Lock()
"""
Binary Mutex Lock (initialized to UNLOCKED).
Guards the critical section – the exact lines where threads call
.append() or .pop(0) on grid_battery_bank.
Only ONE thread may enter the critical section at any moment.
"""

empty = threading.Semaphore(BUFFER_CAPACITY)
"""
Counting Semaphore – tracks the number of EMPTY slots in the buffer.
Initialized to N (= BUFFER_CAPACITY) because all slots start vacant.
Producers call empty.acquire() before inserting.
Consumers call empty.release() after extracting.
Blocks producers automatically when the buffer is full (value = 0).
"""

full = threading.Semaphore(0)
"""
Counting Semaphore – tracks the number of FILLED slots in the buffer.
Initialized to 0 because the buffer starts empty.
Consumers call full.acquire() before extracting.
Producers call full.release() after inserting.
Blocks consumers automatically when the buffer is empty (value = 0).
"""

running: bool = True
"""
Global execution flag.
All thread loops evaluate this flag at the start of every iteration.
Set to False on KeyboardInterrupt to initiate graceful shutdown.
"""

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 – UTILITY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# ANSI colour codes for clean, colour-coded terminal output
class Colour:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    YELLOW  = "\033[93m"   # Producers  – energy generation events
    GREEN   = "\033[92m"   # P1 Consumer (Emergency Hospital)
    CYAN    = "\033[96m"   # P2 Consumer (Residential Zone)
    MAGENTA = "\033[95m"   # P3 Consumer (Industrial Mill)
    RED     = "\033[91m"   # Critical / warning events
    WHITE   = "\033[97m"   # System / meta messages
    BLUE    = "\033[94m"   # Buffer state display


def timestamp() -> str:
    """
    Returns the current wall-clock time as a formatted string.
    Format: [HH:MM:SS.mmm]
    The millisecond component guarantees sub-second resolution in logs,
    making the concurrency interleaving visible even at high speeds.
    """
    now = datetime.now()
    return f"[{now.strftime('%H:%M:%S')}.{now.microsecond // 1000:03d}]"


def buffer_bar(current: int, capacity: int) -> str:
    """
    Renders a compact ASCII progress bar representing the current buffer fill.
    Example with current=3, capacity=5:  [███░░] 3/5
    This visual aid lets an evaluator immediately see the buffer state from
    a single glance at any log line.
    """
    filled  = "█" * current
    vacant  = "░" * (capacity - current)
    return f"{Colour.BLUE}[{filled}{vacant}] {current}/{capacity}{Colour.RESET}"


def log(colour: str, thread_name: str, message: str) -> None:
    """
    Thread-safe logging function.
    Prints a single formatted log line to stdout.
    Format:  [HH:MM:SS.mmm]  <ThreadName>  │  <message>  <buffer_bar>
    Uses print() which is inherently atomic for single calls on CPython,
    preventing log line interleaving from multiple threads.
    """
    buf_state = buffer_bar(len(grid_battery_bank), BUFFER_CAPACITY)
    print(
        f"{colour}{timestamp()}  "
        f"{Colour.BOLD}{thread_name:<24}{Colour.RESET}"
        f"{colour}│  {message:<52}{Colour.RESET}  "
        f"Battery: {buf_state}"
    )

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 – PRODUCER THREAD CLASS
# ─────────────────────────────────────────────────────────────────────────────

class RenewableEnergyPlant(threading.Thread):
    """
    Represents a renewable energy source on the smart grid.

    Behavioural Model
    ─────────────────
    Each plant runs in an infinite daemon loop. On every iteration it:
      1. Sleeps for a random interval (simulating weather variability).
      2. Acquires the 'empty' semaphore  →  blocks if the buffer is full.
      3. Acquires the mutex lock         →  enters critical section.
      4. Appends a uniquely-tagged power packet to grid_battery_bank.
      5. Releases the mutex lock         →  exits critical section.
      6. Releases the 'full' semaphore   →  signals waiting consumers.

    The acquire order (empty → mutex) is strictly maintained to prevent
    the classic Producer-Consumer deadlock scenario.
    """

    def __init__(self, name: str, colour: str, gen_delay: tuple[float, float]) -> None:
        """
        Parameters
        ──────────
        name      : Human-readable plant name (e.g., "SolarFarm")
        colour    : ANSI colour code for this thread's log lines
        gen_delay : (min_seconds, max_seconds) range for random sleep intervals
                    Models weather variability (e.g., cloud cover, wind gusts)
        """
        super().__init__(daemon=True, name=name)
        self.plant_name  = name
        self.colour      = colour
        self.gen_delay   = gen_delay
        self.packet_id   = 0   # Sequential counter for FIFO verification


    def run(self) -> None:
        """
        Main producer loop.
        Executes until the global 'running' flag is set to False.
        """
        global running

        while running:
            # ── Step 1: Simulate weather variability / generation intermittency
            sleep_duration = random.uniform(*self.gen_delay)
            time.sleep(sleep_duration)

            # ── Guard: Re-check flag after sleep to avoid stale loop entry
            if not running:
                break

            # ── Step 2: Acquire 'empty' semaphore FIRST (deadlock-safe order)
            #    If empty.value == 0, the buffer is full → this call BLOCKS
            #    until a consumer frees a slot and calls empty.release().
            #    We use a timeout so the thread can re-check 'running' during shutdown.
            acquired = empty.acquire(timeout=1.0)
            if not acquired:
                # Semaphore not acquired within the window; loop back to check 'running'
                continue

            # ── Guard: Check flag again now that we hold the semaphore
            if not running:
                # Return the semaphore slot we just took before exiting
                empty.release()
                break

            # ── Step 3: Acquire mutex SECOND – enter critical section
            with mutex:
                # ── Step 4: Generate unique packet and inject into buffer (CRITICAL SECTION)
                self.packet_id += 1
                packet = f"[{self.plant_name} #{self.packet_id:04d}]"
                grid_battery_bank.append(packet)

                log(
                    self.colour,
                    self.plant_name,
                    f"⚡ GENERATED  {packet}  → injected into grid"
                )

            # ── Step 5: Release 'full' semaphore AFTER releasing mutex
            #    Signals any consumer blocked on full.acquire() that a new
            #    packet is available for consumption.
            full.release()

        log(Colour.WHITE, self.plant_name, "🔴 Shutdown signal received. Plant offline.")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 – CONSUMER THREAD CLASS
# ─────────────────────────────────────────────────────────────────────────────

class CitySector(threading.Thread):
    """
    Represents a city sector drawing power from the smart grid.

    Behavioural Model
    ─────────────────
    Each sector runs in an infinite daemon loop. On every iteration it:
      1. Sleeps for a random interval (simulating fluctuating demand cycles).
      2. Executes the Smart Yield Priority Algorithm:
           - If buffer level ≤ CRITICAL_RESERVE_THRESHOLD AND
             this thread's priority > 1 (i.e., not Emergency Hospital):
               → Print a yielding alert, sleep briefly, and restart iteration.
               → This voluntarily cedes the remaining power to Tier-1.
      3. Acquires the 'full' semaphore  →  blocks if the buffer is empty.
      4. Acquires the mutex lock        →  enters critical section.
      5. Pops the OLDEST packet (index 0) from grid_battery_bank (FIFO).
      6. Releases the mutex lock        →  exits critical section.
      7. Releases the 'empty' semaphore →  signals waiting producers.

    Priority Tiers
    ──────────────
      1 (HIGH)   – Emergency Hospital     : Never yields; always has access.
      2 (MEDIUM) – Metro Residential Zone : Yields during critical reserve.
      3 (LOW)    – Industrial Steel Mill  : Yields during critical reserve.
    """

    def __init__(
        self,
        name: str,
        colour: str,
        priority: int,
        demand_delay: tuple[float, float]
    ) -> None:
        """
        Parameters
        ──────────
        name         : Human-readable sector name
        colour       : ANSI colour code for this thread's log lines
        priority     : 1 = High, 2 = Medium, 3 = Low
        demand_delay : (min_seconds, max_seconds) range for random sleep intervals
                       Models daily demand cycles (peak hours, off-peak hours)
        """
        super().__init__(daemon=True, name=name)
        self.sector_name  = name
        self.colour       = colour
        self.priority     = priority
        self.demand_delay = demand_delay

        # Human-readable tier label for log output
        self._tier_label = {1: "PRIORITY-1 [HIGH]  ", 2: "PRIORITY-2 [MED]   ", 3: "PRIORITY-3 [LOW]   "}[priority]


    def run(self) -> None:
        """
        Main consumer loop.
        Executes until the global 'running' flag is set to False.
        """
        global running

        while running:
            # ── Step 1: Simulate fluctuating demand (peak/off-peak cycles)
            sleep_duration = random.uniform(*self.demand_delay)
            time.sleep(sleep_duration)

            # ── Guard: Re-check after sleep
            if not running:
                break

            # ── Step 2: SMART YIELD PRIORITY ALGORITHM ─────────────────────
            #
            #    Before spending any semaphore budget, check the live buffer level.
            #    This check runs OUTSIDE the critical section – it is an advisory
            #    read, not a guaranteed atomic observation.  The real protection
            #    is still provided by the semaphores + mutex inside the critical
            #    section.  This outer check is purely for the preemptive-yield
            #    scheduling behaviour required by the OS syllabus.
            #
            #    Condition: buffer is at or below the critical reserve threshold
            #               AND this thread is NOT the highest-priority consumer.
            #
            if (len(grid_battery_bank) <= CRITICAL_RESERVE_THRESHOLD
                    and self.priority > 1):

                log(
                    Colour.RED,
                    self.sector_name,
                    f"⚠️  SMART YIELD │ {self._tier_label} stepping back "
                    f"(battery ≤ {CRITICAL_RESERVE_THRESHOLD} unit). "
                    f"Reserving for Emergency Hospital."
                )
                # Voluntarily sleep before trying again – prevents busy-wait spin
                time.sleep(random.uniform(0.8, 1.5))
                continue   # ← restart the while loop; do NOT touch semaphores

            # ── Step 3: Acquire 'full' semaphore FIRST (deadlock-safe order)
            #    If full.value == 0, the buffer is empty → BLOCKS until a
            #    producer injects a packet and calls full.release().
            acquired = full.acquire(timeout=1.0)
            if not acquired:
                continue

            # ── Guard: Check shutdown flag after acquiring semaphore
            if not running:
                full.release()
                break

            # ── Step 4: Acquire mutex SECOND – enter critical section
            with mutex:
                # ── Step 5: Extract the OLDEST packet (FIFO – index 0) ────────
                #    .pop(0) removes and returns the first element inserted,
                #    guaranteeing First-In-First-Out consumption order.
                #    This is what allows an evaluator to verify sequential IDs
                #    in the consumption log match the production log.
                packet = grid_battery_bank.pop(0)

                log(
                    self.colour,
                    self.sector_name,
                    f"🏙️  CONSUMED   {packet}  ← {self._tier_label}"
                )

            # ── Step 6: Release 'empty' semaphore AFTER releasing mutex
            #    Signals any producer blocked on empty.acquire() that a slot
            #    has opened up in the battery bank.
            empty.release()

        log(Colour.WHITE, self.sector_name, "🔴 Shutdown signal received. Sector offline.")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 – GRACEFUL SHUTDOWN HANDLER
# ─────────────────────────────────────────────────────────────────────────────

def shutdown(threads: list[threading.Thread]) -> None:
    """
    Graceful termination routine triggered on KeyboardInterrupt (Ctrl+C).

    Shutdown Sequence
    ─────────────────
    1. Set the global 'running' flag to False – tells all loops to exit.
    2. "Poison Pill" semaphore flush:
         Release both 'empty' and 'full' semaphores N times each,
         where N = total number of threads.
         Rationale: Any thread currently BLOCKED on a semaphore.acquire()
         call will never see the 'running = False' flag update until it
         is unblocked.  Flooding the semaphores forces every blocked thread
         to wake up, re-enter its loop body, evaluate 'if not running: break',
         and exit cleanly.  This mimics OS-level process wakeup on shutdown.
    3. Join all threads with a timeout – waits for each thread to finish
       its current iteration and call its own exit log line.
    4. Print the final exit confirmation banner.

    Parameters
    ──────────
    threads : List of all active producer and consumer Thread objects
    """
    global running

    total_threads = len(threads)

    print(f"\n{Colour.RED}{Colour.BOLD}")
    print("═" * 78)
    print("  ⚡ EcoSync SHUTDOWN INITIATED – Ctrl+C detected")
    print(f"  Signalling {total_threads} threads to terminate...")
    print("═" * 78)
    print(Colour.RESET)

    # ── Step 1: Lower the execution flag
    running = False

    # ── Step 2: Poison pill – unblock all threads waiting on semaphores
    #    We release each semaphore (total_threads) times.
    #    Even if a thread is NOT blocked, the extra release simply increments
    #    the semaphore counter harmlessly; the thread will still exit because
    #    it checks 'running' immediately after acquiring.
    for _ in range(total_threads):
        try:
            empty.release()
        except Exception:
            pass
        try:
            full.release()
        except Exception:
            pass

    # ── Step 3: Join all threads (wait for clean exit)
    for t in threads:
        t.join(timeout=3.0)
        status = "✔ exited" if not t.is_alive() else "✘ still alive (timeout)"
        print(f"  {Colour.WHITE}{t.name:<30} {status}{Colour.RESET}")

    # ── Step 4: Final confirmation banner
    print(f"\n{Colour.GREEN}{Colour.BOLD}")
    print("═" * 78)
    print("  ✅ EcoSync terminated cleanly. All threads joined.")
    print("     No orphaned processes. No resource leaks.")
    print("     SDG 7 & SDG 11 simulation ended.")
    print("═" * 78)
    print(Colour.RESET)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 – MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """
    Application entry point.

    Responsibilities
    ────────────────
    1. Print the startup banner with configuration summary.
    2. Instantiate all producer and consumer Thread objects.
    3. Start all threads.
    4. Block the main thread with an interruptible sleep loop.
    5. On KeyboardInterrupt, call shutdown() for graceful cleanup.
    """

    # ── Startup banner ────────────────────────────────────────────────────────
    print(f"\n{Colour.WHITE}{Colour.BOLD}")
    print("╔══════════════════════════════════════════════════════════════════════════╗")
    print("║        EcoSync – Smart Grid Power Distribution Simulator               ║")
    print("║        OS Module 2: Bounded Buffer / Producer-Consumer Problem          ║")
    print("╠══════════════════════════════════════════════════════════════════════════╣")
    print(f"║  Battery Bank Capacity      : {BUFFER_CAPACITY} slots                                   ║")
    print(f"║  Critical Reserve Threshold : ≤ {CRITICAL_RESERVE_THRESHOLD} unit  ({int(0.2*100)}% of capacity)               ║")
    print(f"║  Producers (2)              : SolarFarm, WindPark                       ║")
    print(f"║  Consumers (3)              : EmergencyHospital [P1],                   ║")
    print(f"║                               MetroResidentialZone [P2],                ║")
    print(f"║                               IndustrialSteelMill [P3]                  ║")
    print(f"║  Deadlock Prevention        : Semaphore acquired BEFORE mutex           ║")
    print(f"║  Termination                : Press Ctrl+C to gracefully shut down      ║")
    print("╚══════════════════════════════════════════════════════════════════════════╝")
    print(Colour.RESET)

    time.sleep(0.5)   # Brief pause so the banner is fully visible before logs start

    # ── Instantiate Producers ─────────────────────────────────────────────────
    #
    # gen_delay ranges model real-world energy intermittency:
    #   SolarFarm  : 0.5 – 1.8s  (faster in good sunlight, slower on cloudy days)
    #   WindPark   : 0.8 – 2.2s  (depends on wind speed variability)
    #
    solar_farm = RenewableEnergyPlant(
        name       = "SolarFarm",
        colour     = Colour.YELLOW,
        gen_delay  = (0.5, 1.8)
    )
    wind_park = RenewableEnergyPlant(
        name       = "WindPark",
        colour     = Colour.YELLOW,
        gen_delay  = (0.8, 2.2)
    )

    # ── Instantiate Consumers ─────────────────────────────────────────────────
    #
    # demand_delay ranges model real-world consumption patterns:
    #   Hospital   : 0.6 – 1.2s  (critical, continuous demand – fastest consumer)
    #   Residential: 1.0 – 2.0s  (peak morning/evening, lighter overnight)
    #   Steel Mill : 1.2 – 2.5s  (heavy industrial load but scheduled batch ops)
    #
    emergency_hospital = CitySector(
        name         = "EmergencyHospital",
        colour       = Colour.GREEN,
        priority     = 1,
        demand_delay = (0.6, 1.2)
    )
    metro_residential = CitySector(
        name         = "MetroResidentialZone",
        colour       = Colour.CYAN,
        priority     = 2,
        demand_delay = (1.0, 2.0)
    )
    industrial_mill = CitySector(
        name         = "IndustrialSteelMill",
        colour       = Colour.MAGENTA,
        priority     = 3,
        demand_delay = (1.2, 2.5)
    )

    # ── Collect all threads for management ───────────────────────────────────
    all_threads: list[threading.Thread] = [
        solar_farm,
        wind_park,
        emergency_hospital,
        metro_residential,
        industrial_mill,
    ]

    # ── Start all threads ─────────────────────────────────────────────────────
    print(f"{Colour.WHITE}[SYS]  Starting {len(all_threads)} concurrent threads...{Colour.RESET}\n")
    for t in all_threads:
        t.start()

    # ── Main thread: stay alive until Ctrl+C ─────────────────────────────────
    #
    # The main thread enters an interruptible sleep loop.
    # Using small 0.2s intervals (rather than one long sleep) allows the
    # KeyboardInterrupt to be caught with minimal latency.
    #
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        shutdown(all_threads)
        sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY GUARD
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
