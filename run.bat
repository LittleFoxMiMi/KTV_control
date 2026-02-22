start "fastapi" ./python/python.exe main.py
start "huey" ./python/python.exe worker.py
start "node" cmd /k "cd ./front_end && npm run dev"
