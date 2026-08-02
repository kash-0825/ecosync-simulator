\# EcoSync — Multithreaded Smart Grid Power Distribution Simulator



A Python simulation of a smart grid battery bank, demonstrating the classic bounded-buffer producer-consumer problem using multithreading, semaphores, and mutex locks.



\## Overview



EcoSync models a shared battery bank (bounded buffer) where multiple producer threads generate power packets and multiple consumer threads draw from them. Access to the shared buffer is synchronized to prevent race conditions, and a dynamic "Smart Yield" mechanism ensures only priority-1 consumers can draw power once the buffer drops to a critical reserve threshold.



\## How it works



\- \*\*Bounded buffer\*\*: `grid\_battery\_bank` holds a limited number of power packets (`BUFFER\_CAPACITY`).

\- \*\*Synchronization\*\*: semaphores and a mutex lock coordinate access between producer and consumer threads, preventing overproduction, overconsumption, and race conditions.

\- \*\*Critical reserve threshold\*\*: dynamically calculated as 20% of buffer capacity. When the buffer drops to or below this value, only Priority-1 threads are allowed to consume — all lower-priority threads yield.



\## Tech Stack



\- Python 3

\- `threading` — concurrent producer/consumer threads

\- Semaphores and mutex locks for synchronization



\## Running it



```bash

python ecosync\_simulator.py

```



The simulation prints real-time buffer activity to the console, including producer/consumer actions and yield events under critical reserve conditions.



\## Notes



\- Fixes UTF-8 encoding issues (`UnicodeEncodeError`) that occur when running in terminals defaulting to CP-1252 (e.g. Windows `cmd.exe`).

\- Includes an ANSI color fallback for terminals like VS Code's Code Runner that don't natively support ANSI escape codes on Windows.

