"""Worker side of the pipeline: consume job ids, run the processing step, and
own every status write after the API's initial insert."""
