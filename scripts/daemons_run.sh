#!/bin/bash
#
#    * daemons_run test script *
#
#    With this script it is possible to run N full daemons on
#    localhost and had them connect to each other.
#
#    They all run with UPnP disabled (as they don't need to talk
#    to the Internet) and they are all initialized with M
#    known nodes in order to bootstrap the DHT.
#
#    The script can then be used to control the daemons via the
#    cli tool.
#
#    When launched, the script will create a folder named
#    "daemons_run_tmp". For each of the N daemons, in this folder
#    you will find:
#    - a data dir
#    - a config file (.json)
#    - a log file containing stdout/err (.log)
#    - a pid file (.pid)
#
#    The script can lastly be used to to stop/kill the running
#    daemons.
#
#    N and M are hardcoded in the script and have a default value
#    of N=10 and M=3
#
#


NODES_NUM=10
KNOWN_NUM=3

VERBOSE_CLI=0

TMP_FOLDER=daemons_run_tmp

function check_folder_() {
if [ ! -d "${TMP_FOLDER}" ]; then
    echo "Folder ${TMP_FOLDER} does not exist!"
    exit 1
fi
}

function start1() {
    N=${1}
    if [ -z "${N}" ]; then
        echo "no daemon specified"
        exit 1
    fi

    echo "Creating config file ${N}"

    DHT_PORT=$(( 3300 + ${N} ))
    PEER_PORT=$(( 4400 + ${N} ))
    API_PORT=$(( 5200 + ${N} ))

    unset KNOWN_NODES
    for j in $(seq 1 ${KNOWN_NUM}); do
        P=$((3300 + $(( (RANDOM % ${NODES_NUM}) + 1 )) ))
        [ x"${KNOWN_NODES}" != x ] && KNOWN_NODES="${KNOWN_NODES}, "
        KNOWN_NODES="${KNOWN_NODES}\"127.0.0.1:${P}\""
    done

cat > ${TMP_FOLDER}/${N}.json << EOF
{
    "use_upnp" : false,
    "external_ip" : "127.0.0.1",
    "peer_port" : ${PEER_PORT},
    "dht_node_port" : ${DHT_PORT},
    "data_dir" : "$(pwd)/${TMP_FOLDER}/${i}/data",
    "download_directory" : "$(pwd)/${TMP_FOLDER}/${i}/download",
    "lbryum_wallet_dir" : "$(pwd)/${TMP_FOLDER}/${i}/wallet",
    "known_dht_nodes": [${KNOWN_NODES}],
    "api_port": ${API_PORT}
}
EOF

    mkdir -p ${TMP_FOLDER}/${N}/data
    mkdir -p ${TMP_FOLDER}/${N}/download
    mkdir -p ${TMP_FOLDER}/${N}/wallet

    echo "Launching daemon ${N}"

    lbrynet-daemon --conf ${TMP_FOLDER}/${N}.json --verbose 2>&1 >${TMP_FOLDER}/${N}.log &
    echo $! >${TMP_FOLDER}/${N}.pid
}

function start() {
    echo "STARTING DAEMONS"

    mkdir -p ${TMP_FOLDER}

    for i in $(seq 1 ${NODES_NUM}); do
        echo -n "${i}."
        start1 ${i}
    done
    echo "done"
}

function signal1_() {
    N=${1}
    SIG=${2}
    if [ -z "${SIG}" -o -z "${N}" ]; then
        echo "no signal or daemon specified!"
        exit 1
    fi

    check_folder_ ${N}
    /bin/kill -${SIG} $(cat ${TMP_FOLDER}/${N}.pid)
}

function signal_() {
    SIG=${1}
    if [ -z "${SIG}" ]; then
        echo "no signal specified"
        exit 1
    fi

    echo "Sending SIG${1} to daemons"
    for i in $(seq 1 ${NODES_NUM}); do
        echo -n "${i}."
        signal1_ ${i} ${SIG}
    done
    echo "done"
}

function stop() {
    echo "STOPPING DAEMONS"
    signal_ TERM
}

function kill() {
    echo "KILLING DAEMONS"
    signal_ KILL
}

function stop1() {
    N=${1}
    if [ -z "${N}" ]; then
        echo "no daemon specified"
        exit 1
    fi

    echo "SENDING SIGTERM TO DAEMON ${N}"
    signal1_ ${N} TERM
}

function kill1() {
    N=${1}
    if [ -z "${N}" ]; then
        echo "no daemon specified"
        exit 1
    fi

    echo "SENDING SIGKILL TO DAEMON ${N}"
    signal1_ ${N} KILL
}

function cli1() {
    N=${1}
    if [ -z "${N}" ]; then
        echo "no daemon specified!" >&2
        exit 1
    fi

    check_folder_ ${N}
    shift

    [ ${VERBOSE_CLI} -gt 0 ] && echo "Executing CLI command on daemon ${N}: $@"
    CMD="lbrynet-cli --conf ${TMP_FOLDER}/${N}.json $@"
    [ ${VERBOSE_CLI} -gt 0 ] && echo "Daemon ${N}, running: ${CMD}"
    ${CMD}
}

function cli() {
    echo "EXECUTING CLI COMMAND: $@"

    for i in $(seq 1 ${NODES_NUM}); do
        echo "Daemon ${i}:"
        cli1 ${i} $@
    done
}

function check_blob() {
    BLOB=${1}
    if [ -z "${BLOB}" ]; then
        echo "missing blob hash"
        exit 1
    fi

    # pick a random daemon that will announce the blob
    SRC="$(( (RANDOM % ${NODES_NUM}) + 1 ))"

    echo "Injecting blob ${BLOB} on daemon ${SRC}..."
    cli1 ${SRC} blob_announce ${BLOB}
    echo "done"

    echo "Waiting 5 seconds..."
    sleep 5

    echo "Checking peer_list on every daemon..."
    T=.tmp_test1
    rm -Rf ${T}
    mkdir -p ${T}
    for i in $(seq 1 ${NODES_NUM}); do
        echo -n "${i}."
        cli1 ${i} peer_list ${BLOB} >${T}/${i}.list
    done
    echo "done"

    echo "Comparing files"
    diff -u --from-file ${T}/${SRC}.list ${T}/*.list
    if [ $? -ne 0 ]; then
        echo "peer lists differs!"
        exit 1
    else
        echo "all peer lists consistent"
        exit 0
    fi
}

if [ x"$(type -t ${1})" != x"function" ]; then
    echo "Invalid command"
    echo "Usage $0 <start|stop|kill|cli <cmd ...>|cli1 <n> <cmd ...>>"
    echo "  start           Start the daemons"
    echo "  start1 <n>      Start only daemon 'n'"
    echo
    echo "  stop            Stop daemons by sending SIGTERM (soft termination)"
    echo "  stop1 <n>       Stop only daemon 'n'"
    echo
    echo "  kill            Stop daemons by sending SIGKILL (hard termination)"
    echo "  kill1 <n>       Kill only daemon 'n'"
    echo
    echo "  cli <cmd ...>   Send command to daemons using the cli tool."
    echo "                  The entire set of arguments 'cmd ...' is"
    echo "                  passed as is to the cli tool."
    echo
    echo "  cli1 <n> ...    Like above but send command only to daemon 'n'"
    exit 1
fi

FUNC=${1}
shift
${FUNC} $@
