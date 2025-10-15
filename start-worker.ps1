param(
    [string]$ImageName = "twilio-worker",
    [string]$WorkerContainer = "rq-worker",
    [string]$RedisContainer = "redis",
    [string]$NetworkName = "twilio-net",
    [switch]$BuildIfMissing = $true,
    [string]$RedisUrl = "redis://redis:6379/0",
    [string]$WorkerCmd = "rq worker transcribe --url redis://redis:6379/0 --verbose"
)

function ExitWithError($msg){
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

Write-Host "== start-worker.ps1: ensure network, redis, remove old worker, start worker (detached) and tail logs ==" -ForegroundColor Cyan

# ensure docker is available
try {
    $dockerVer = & docker --version 2>$null
} catch {
    ExitWithError "Docker not found. Make sure Docker Desktop is installed and 'docker' is in PATH."
}
Write-Host "Docker: $dockerVer"

# create network if missing
$networkExists = (& docker network ls --filter name="^$NetworkName$" --format "{{.Name}}") -contains $NetworkName
if (-not $networkExists) {
    Write-Host "Creating network: $NetworkName"
    & docker network create $NetworkName | Out-Null
    if ($LASTEXITCODE -ne 0) { ExitWithError "Failed to create network $NetworkName" }
} else {
    Write-Host "Network exists: $NetworkName"
}

# ensure redis container is running
$redisExists = (& docker ps -a --format "{{.Names}}" | Select-String -Pattern "^$RedisContainer$") -ne $null
if (-not $redisExists) {
    Write-Host "Starting Redis container '$RedisContainer' on network '$NetworkName'..."
    & docker run -d --name $RedisContainer --network $NetworkName redis:latest | Out-Null
    if ($LASTEXITCODE -ne 0) { ExitWithError "Failed to start Redis container" }
} else {
    $redisRunning = (& docker ps --format "{{.Names}}" | Select-String -Pattern "^$RedisContainer$") -ne $null
    if ($redisRunning) {
        Write-Host "Redis container '$RedisContainer' already running"
    } else {
        Write-Host "Starting existing Redis container '$RedisContainer'..."
        & docker start $RedisContainer | Out-Null
        if ($LASTEXITCODE -ne 0) { ExitWithError "Failed to start Redis container" }
    }
}

# optional: build image if missing
$imageExists = (& docker images --format "{{.Repository}}:{{.Tag}}" | Where-Object { $_ -like "$ImageName*" }) -ne $null
if (-not $imageExists -and $BuildIfMissing) {
    if (Test-Path "Dockerfile.worker") {
        Write-Host "Image '$ImageName' not found locally. Building using Dockerfile.worker..."
        & docker build -f Dockerfile.worker -t $ImageName .
        if ($LASTEXITCODE -ne 0) { ExitWithError "Docker build failed. Check output." }
    } else {
        Write-Host "Image '$ImageName' not found and Dockerfile.worker missing. Skipping build."
    }
} elseif ($imageExists) {
    Write-Host "Image '$ImageName' found locally."
} else {
    Write-Host "BuildIfMissing is false and image not found. Proceeding."
}

# remove existing worker container safely
$workerExists = (& docker ps -a --format "{{.Names}}" | Select-String -Pattern "^$WorkerContainer$") -ne $null
if ($workerExists) {
    Write-Host "Stopping and removing existing container '$WorkerContainer'..."
    & docker stop $WorkerContainer | Out-Null
    & docker rm $WorkerContainer | Out-Null
}

# run worker detached
$runArgs = @("run", "-d", "--name", $WorkerContainer, "--network", $NetworkName, $ImageName) 
# we will append the worker command as separate args (split on space)
$cmdParts = $WorkerCmd -split ' '
$fullArgs = $runArgs + $cmdParts

Write-Host "Starting worker container (detached): docker $($fullArgs -join ' ')" -ForegroundColor DarkGray
& docker @fullArgs
if ($LASTEXITCODE -ne 0) { ExitWithError "Failed to start worker container." }

# give a short moment and tail logs
Start-Sleep -Seconds 1
Write-Host "`nTailing logs for $WorkerContainer (CTRL+C to stop):" -ForegroundColor Green
& docker logs -f $WorkerContainer
