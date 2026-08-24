@echo off
rem ---------------------------------------------------------------
rem  Weekly checkup - wakes the deployed Supabase DB and appends
rem  one line to results/weekly log.  Registered in Windows Task
rem  Scheduler:  every Monday 10:00.
rem
rem  %~dp0 = the folder this .bat file lives in.  Using it instead
rem  of a hard-coded path keeps the absolute path out of the repo
rem  and makes the file work if the project moves.
rem
rem  Task Scheduler starts programs in C:\Windows\System32, so the
rem  "cd /d" is required - without it Python cannot find src/.
rem ---------------------------------------------------------------
cd /d "%~dp0"
".venv\Scripts\python.exe" -m src.weekly 2>> "results\weekly-error.log"
