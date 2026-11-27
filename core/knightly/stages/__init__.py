"""The stages of the app, each with its own settings section, inputs and outputs (tables of the cache):

extract (games) → annotate (moves) → select (puzzle candidates) → build (puzzles, notes) → manage (the notes
already in the collection). For now `jobs.run_update` runs extract, then the rest in `pipeline.Miner`.
"""
