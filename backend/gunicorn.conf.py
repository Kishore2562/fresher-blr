   # Read automatically by gunicorn from the backend folder.
   workers = 1      # keep 1: the posting limit is kept in memory
   threads = 8      # many phones can poll at the same time
   timeout = 120    # do not kill the worker while the database wakes up