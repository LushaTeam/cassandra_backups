`cassandra_backups` is a tool to backup a Cassandra cluster using nodetool snapshots and 
incremental backups on S3.

The scope of this project is to make it easier to backup a cluster to S3 and to combine
snapshots and incremental backups.

This is a fork of https://github.com/tbarbugli/cassandra_snapshotter 
Some differences with the original, as of 11/2016:
 
 * Fixed several issues: one critical affecting restore, another affecting backups (present on 
 the last version of `cassandra_snapshotter` in PYPI at the time of writing), and a few others.
 
 * Restore command can be executed locally: 
    Agents running in the nodes connect to the specified S3 bucket and dump the data to 
    `/tmp/cassandra_restore`. 
    * **IMPORTANT!** Restore does _not_ overwrite Cassandra directories.
    
    * When restoring from multiple nodes, each node fetches the data it uploaded itself.
     Then the "Node Restart Method" can be executed on each node.

 * Backups follow a `YearMonthDay` format instead of timestamp.
    * New directories can be created every day as a result of scheduled backups, so that if (e.g.)
    data from last day is known to be corrupted, a previous backup can be restored.
    Within a day, backups after the first one are incremental.
    
    * It is trivial to change that format to something more convenient:
    just grep `SNAPSHOT_TIMESTAMP_FORMAT` in the codebase and change the format.

 * Added a `user` option for using together with `sudo`, renamed some other options, and changed 
 some defaults to match what Cassandra 3.X expects.
 

Limitations and Recommendations
===============================

* Backup / Restore a multi-node cluster with this and the "Node Restart Method" generally works,
but many options are untested (eg: operations scoped to a single column family).
    
* It is possible to backup all keyspaces at once, but restore has to be done one by one.

* Likewise, it is possible to backup from several host with a single command, but to restore 
you need to execute one command per host you need to restore into.

* Since you might need to pass the AWS credentials to this program, it is a good idea to create
an AMI role with the minimum permissions required (S3 read/write).

* Old S3 folders are not automatically cleaned up.

* Make sure you run this program with the right user and user options, so the restored data will 
have the same owner and permissions as the original data you backed up.

* Restore a backup is a delicate operation, make sure to test it exhaustively! 


Install
=======

`pip install git+https://github.com/LushaTeam/cassandra_backups.git`

It needs to be installed in your local (where you will run the command) and in all
the Cassandra nodes.

You also need `lzop`. On Debian/Ubuntu: 
`sudo apt-get install lzop`

Other dependencies that you might not have:
`sudo apt-get install libffi-dev g++ libssl-dev libyaml-dev libpython2.7-dev`

  
Usage Examples
==============

_Backup_

```
cassandra-backups 
    --s3-bucket-name=cassandra_snapshots
    --s3-bucket-region=us-east-1 
    --s3-base-path=webapp_prod
    --s3-ssenc
    --aws-access-key-id=XXXXXXXXXXXXXXXXX 
    --aws-secret-access-key=xxxxxxxxxxxxxxxxxxxxxxxxx 
    backup 
    --hosts=cassandra_node_01.domain.com,cassandra_node02_domain.com 
    --use-sudo=true
    --user=ubuntu
```


_Restore_

```
cassandra-backups 
    --s3-bucket-name=cassandra_snapshots 
    --s3-bucket-region=us-east-1 
    --s3-base-path=webapp_prod 
    --aws-access-key-id=XXXXXXXXXXXXXXXXX 
    --aws-secret-access-key=xxxxxxxxxxxxxxxxxxxxxxxxx 
    restore 
    --keyspace=user_cf
    --host=cassandra_node_01.domain.com 
    --user=ubuntu 
    --use-sudo=true 
    --sudo-user=cassandra
```


If you want incremental backups, remember to set the flag in the `cassandra.yaml` config file.


The Node Restart Method
=======================

After backup / restore operations, the Node Restart Method can be applied.

Official documentation at the time of writing for Cassandra 2.1 is here:
https://docs.datastax.com/en/cassandra/2.1/cassandra/operations/ops_backup_snapshot_restore_t.html

A general approach, after using this tool to backup and restore, goes like this: 
(assuming Cassandra 3.0 installed on `/var/lib/cassandra/data`) 
 
* Check that `/var/lib/cassandra/data/KEYSPACE` and `/tmp/cassandra_restore/KEYSPACE` list
  the same folders. Those folders represent column families with an id. Those ids have to match.
  They change, for instance, when you `DROP` a column family or you add a node to the cluster.
  Old unused ids are kept around in `/var/lib/cassandra/data/KEYSPACE` and never removed as part
  of any `nodetool` or `cqlsh` command (to my knowledge). It is safe to `rm -rf` them.
  You can run this query to check the current ids in use:
  `select id from system_schema.tables where keyspace_name = KEYSPACE;`
  
* Check that files in `/tmp/cassandra_restore/KEYSPACE/COLUMN_FAMILY` have an expectable size.

* Stop all the nodes, and then, one by one, run:
 `rm -rf /var/lib/cassandra/commitlog/*`
 and:
 `cd /tmp/cassandra_restore/KEYSPACE/COLUMN_FAMILY;`
 `ls * | xargs mv -t  /var/lib/cassandra/data/KEYSPACE/COLUMN_FAMILY`
 for every `COLUMN_FAMILY` (and `KEYSPACE`, if needed)
 `xargs` will make sure that `mv` works regardless of how many files you have in the directory.
 
* Restart all the nodes, run `nodetool repair`, and perform some sanity checks.
You can (should!) do it all with a tool like Fabric to avoid typing mistakes and other nightmares.
   

Disclaimer
==========

This fork has been renamed to avoid naming confusions in the Python Package Index. 
