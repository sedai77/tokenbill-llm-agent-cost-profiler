### fanout.stagger — let the first call write the shared prefix

When an agent fans out parallel calls that share a prefix, send the first call, wait until its
response starts, then send the rest: the later calls read the prefix the first one wrote instead of
each writing it again.
