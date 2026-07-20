#!/bin/bash
# Restart DaVinci Resolve MCP Server and run tests
# This script combines server restart with automated testing

echo "========================================================"
echo "DaVinci Resolve MCP Server - Test After Restart"
echo "========================================================"

# Get the directory where the script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Preflight: report Resolve state (closed / open_no_project / open_project)
# before anything runs, so failures downstream aren't mystery NOT_CONNECTED.
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/../.venv/bin/python}"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN=python3

if ! "$PYTHON_BIN" "$SCRIPT_DIR/preflight.py"; then
    echo "⚠️ Resolve is not ready (see preflight output above)."
    read -p "Start testing anyway? (y/n): " continue_anyway
    if [[ "$continue_anyway" != "y" ]]; then
        echo "Aborting test."
        exit 1
    fi
    echo "Continuing with testing despite Resolve not being ready..."
fi

# Step 1: Restart the server using the restart script
echo "Step 1: Restarting DaVinci Resolve MCP Server..."
if [ -f "./restart-server.sh" ]; then
    ./restart-server.sh
    
    # Check if restart was successful
    sleep 3
    pgrep -f "python.*resolve_mcp_server.py" > /dev/null
    if [ $? -eq 0 ]; then
        echo "✅ Server restart successful"
    else
        echo "❌ Server restart failed. Please check server logs for errors."
        exit 1
    fi
else
    echo "❌ restart-server.sh not found. Cannot restart server."
    exit 1
fi

# Step 2: Create test timeline (optional)
echo
echo "Step 2: Create test timeline with media?"
read -p "Create test timeline? (y/n): " create_timeline
if [[ "$create_timeline" == "y" ]]; then
    echo "Creating test timeline..."
    if [ -f "./create_test_timeline.py" ]; then
        python3 ./create_test_timeline.py
        if [ $? -ne 0 ]; then
            echo "⚠️ Test timeline creation had issues, but we'll continue with testing."
        fi
    else
        echo "❌ create_test_timeline.py not found. Skipping test timeline creation."
    fi
else
    echo "Skipping test timeline creation."
fi

# Step 3: Run automated tests
echo
echo "Step 3: Running automated tests..."
if [ -f "./test_improvements.py" ]; then
    python3 ./test_improvements.py
    TEST_RESULT=$?
    if [ $TEST_RESULT -eq 0 ]; then
        echo "✅ All tests passed!"
    else
        echo "⚠️ Some tests failed. Check the logs for details."
    fi
else
    echo "❌ test_improvements.py not found. Cannot run tests."
    exit 1
fi

echo
echo "========================================================"
echo "Testing complete. Results logged to mcp_test_results.log"
echo "========================================================"

exit $TEST_RESULT 