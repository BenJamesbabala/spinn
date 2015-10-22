#!/bin/bash

### Generic job script for all experiments.

#PBS -l nodes=1:ppn=6 	### Request at least 6 cores
#PBS -l walltime=99:00:00	### Die after four days
#PBS -l mem=6000MB
#PBS -q nlp

# Usage example:
# export REMBED_FLAGS="--learning_rate 0.2"; qsub -v REMBED_FLAGS quant/run.sh

# Change to the submission directory.
cd $PBS_O_WORKDIR
echo Lauching from working directory $PBS_O_WORKDIR
echo Flags: $REMBED_FLAGS

# Log what we're running and where.
echo `hostname` - $PBS_JOBID - $REMBED_FLAGS >> ~/rembed_machine_assignments.txt

python rembed/models/classifier.py $REMBED_FLAGS
