cassandra_snapshotter
======================

A tool to backup cassandra nodes using snapshots and incremental backups on S3

The scope of this project is to make it easier to backup a cluster to S3 and to combine
snapshots and incremental backups.


NOTES
=====

This is a fork of `tbarbugli/cassandra_snapshotter`. Some differences with the original repository:
 
 * Fixed some issues with restoring (relative paths).
 * Restore command can be executed locally: 
    Agents running in the nodes connect to the specified S3 bucket and dump the content into `/tmp/cassandra_restore`. 
    _Restore does not overwrite Cassandra directories directly_
    
 * When restoring from multiple nodes, each node fetches the data it uploaded itself. 
    Then the "Node Restart Method" can be executed on each node.
 * Backups follow a `YearWeeknumber` format instead of timestamp (eg: `201651`).
    New directories are created every week as a result of scheduled backups, so that if (e.g.) 
    data from last week is known to be corrupted, a previous backup can be restored.
    Whitin a week, backups after the first one are incremental. 
 * Added a `user` option for using together with `sudo`.
 * Changed some defaults to match what Cassandra 3.X expects.
 
 
Backup / Restore a multi-node cluster with this and the "Node Restart Method" generally works,
but many options are untested. 
    
 
  
  

