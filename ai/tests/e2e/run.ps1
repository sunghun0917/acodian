param([string]$TestKeyword = '')

$ErrorActionPreference = 'Stop'
$expected = @{
    WORKLOG_NORMAL_E2E_ENABLED = '1'
    WORKLOG_NORMAL_E2E_API_URL = 'http://api:8100/api'
    WORKLOG_NORMAL_E2E_AI_URL = 'http://ai:8200/ai'
    WORKLOG_NORMAL_E2E_BROKER_URL = 'redis://redis:6379/0'
}
foreach ($name in $expected.Keys) {
    if ([Environment]::GetEnvironmentVariable($name) -ne $expected[$name]) {
        throw "Explicit isolated value required: $name"
    }
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$compose = Join-Path $PSScriptRoot 'compose.yml'
$emptyEnv = Join-Path $PSScriptRoot 'empty.env'
$project = 'axwms-e2e'
$runner = 'axwms-e2e-runner-' + [Guid]::NewGuid().ToString('N')
$expectedServices = @('api', 'ai', 'index-worker', 'proxy', 'postgres', 'redis', 'qdrant', 'neo4j')
$expectedNames = @($expectedServices | ForEach-Object { "axwms-e2e-$_-1" }) + $runner
$knownContainers = @{}
$started = $false
$resultCode = 1

function Compose {
    & docker compose --env-file $emptyEnv -p $project -f $compose @args
    if ($LASTEXITCODE -ne 0) { throw "Compose command failed with exit code $LASTEXITCODE" }
}

function ProjectContainers {
    return @(docker ps -aq --filter "label=com.docker.compose.project=$project")
}

function VerifyOwnedResources {
    $ids = @(ProjectContainers)
    foreach ($id in $ids) {
        $container = (docker inspect $id | ConvertFrom-Json)[0]
        $name = $container.Name.TrimStart('/')
        if ($container.Config.Labels.'com.docker.compose.project' -ne $project -or $name -notin $expectedNames) {
            throw 'Unexpected project container; cleanup refused'
        }
        if ($knownContainers.ContainsKey($name) -and $knownContainers[$name] -ne $container.Id) {
            throw 'Project container identity changed; cleanup refused'
        }
        if (-not $knownContainers.ContainsKey($name) -and $name -ne $runner) {
            throw 'Unrecorded project container; cleanup refused'
        }
        foreach ($mount in $container.Mounts) {
            if ($mount.Type -eq 'volume' -and $mount.Name -in $initialVolumes) {
                throw 'Previously existing volume is attached; cleanup refused'
            }
        }
    }
    $networks = @(docker network ls -q --filter "label=com.docker.compose.project=$project")
    if ($networks.Count -gt 1) { throw 'Unexpected project network; cleanup refused' }
    if ($networks.Count -eq 1) {
        $network = (docker network inspect $networks[0] | ConvertFrom-Json)[0]
        if ($network.Name -ne 'axwms-e2e_default' -or -not $network.Internal) {
            throw 'Network identity changed; cleanup refused'
        }
        foreach ($member in $network.Containers.PSObject.Properties.Value) {
            if ($member.Name -notin $expectedNames) { throw 'Unexpected network member; cleanup refused' }
        }
    }
    $volumes = @(docker volume ls -q --filter "label=com.docker.compose.project=$project")
    if (@($volumes | Where-Object { $_ -ne 'axwms-e2e_ai-data' }).Count) {
        throw 'Unexpected project volume; cleanup refused'
    }
}

$initialContainers = @(docker ps -a --format '{{.Names}} {{.ID}}' | Where-Object { $_ -match '^axwms-' -and $_ -notmatch '^axwms-e2e-' })
$initialRunning = @(docker ps --format '{{.Names}} {{.ID}}' | Where-Object { $_ -match '^axwms-' -and $_ -notmatch '^axwms-e2e-' })
$initialVolumes = @(docker volume ls -q)
if (@(ProjectContainers).Count -or
    @(docker ps -a --format '{{.Names}}' | Where-Object { $_ -like 'axwms-e2e-*' }).Count -or
    @(docker network ls -q --filter "label=com.docker.compose.project=$project").Count -or
    @(docker network ls --format '{{.Name}}' | Where-Object { $_ -eq 'axwms-e2e_default' }).Count -or
    @(docker volume ls -q --filter "label=com.docker.compose.project=$project").Count) {
    throw 'E2E project resources already exist; start refused'
}

try {
    Push-Location (Join-Path $root 'api')
    try {
        & .\gradlew.bat bootJar -x test -x jooqGenerate --console=plain
        if ($LASTEXITCODE -ne 0) { throw 'API bootJar failed' }
    } finally { Pop-Location }

    Compose build base-image
    Compose build api ai index-worker runner
    $started = $true
    Compose up -d --no-build --wait --wait-timeout 180
    foreach ($id in @(ProjectContainers)) {
        $container = (docker inspect $id | ConvertFrom-Json)[0]
        $knownContainers[$container.Name.TrimStart('/')] = $container.Id
    }
    VerifyOwnedResources
    $network = (docker network inspect 'axwms-e2e_default' | ConvertFrom-Json)[0]
    $members = @($network.Containers.PSObject.Properties.Value | ForEach-Object { $_.Name })
    if ($members.Count -ne $expectedServices.Count -or @($members | Where-Object { $_ -notin $expectedNames }).Count) {
        throw 'E2E network membership mismatch'
    }
    foreach ($id in @(ProjectContainers)) {
        $container = (docker inspect $id | ConvertFrom-Json)[0]
        $attachedNetworks = @($container.NetworkSettings.Networks.PSObject.Properties.Name)
        if ($attachedNetworks.Count -ne 1 -or $attachedNetworks[0] -ne 'axwms-e2e_default') {
            throw 'Unexpected container network'
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        $status = docker exec axwms-e2e-ai-1 curl -s -o /dev/null -w '%{http_code}' http://api:8100/api/actuator/health
        if ($status -eq '401') { break }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($status -ne '401') { throw 'API readiness deadline expired' }
    Write-Output "Verified internal network, $($members.Count) project members and separate volumes."

    $selection = @()
    if ($TestKeyword) { $selection = @('-k', $TestKeyword) }
    & docker compose --env-file $emptyEnv -p $project -f $compose --profile test run --name $runner --no-deps `
        -e "WORKLOG_NORMAL_E2E_NETWORK_ID=$($network.Id)" runner python -m pytest tests/test_light_normal_flow_e2e.py `
        -q -rs --tb=no --show-capture=no -p no:cacheprovider -o junit_family=legacy --junitxml=/tmp/e2e-result.xml @selection
    $resultCode = $LASTEXITCODE
    $runnerData = (docker inspect $runner | ConvertFrom-Json)[0]
    if ($runnerData.Config.Labels.'com.docker.compose.project' -ne $project -or $runnerData.Name.TrimStart('/') -ne $runner) {
        throw 'Runner identity mismatch'
    }
    $knownContainers[$runner] = $runnerData.Id
    $evidence = Join-Path $PSScriptRoot 'artifacts-normal'
    New-Item -ItemType Directory -Force -Path $evidence | Out-Null
    $junit = Join-Path $evidence ('confirmation-' + [Guid]::NewGuid().ToString('N') + '.xml')
    & docker cp "${runner}:/tmp/e2e-result.xml" $junit
    if ($LASTEXITCODE -ne 0) { throw 'Could not copy JUnit evidence' }
} finally {
    if ($started) {
        VerifyOwnedResources
        Compose down -v --remove-orphans
        $afterContainers = @(docker ps -a --format '{{.Names}} {{.ID}}' | Where-Object { $_ -match '^axwms-' -and $_ -notmatch '^axwms-e2e-' })
        foreach ($line in $initialContainers) {
            if ($line -notin $afterContainers) { throw 'Existing axwms container identity changed' }
        }
        $afterRunning = @(docker ps --format '{{.Names}} {{.ID}}')
        foreach ($line in $initialRunning) {
            if ($line -notin $afterRunning) { throw 'Existing axwms container is no longer running' }
        }
        Write-Output 'E2E resources removed; pre-existing axwms container identities preserved.'
    }
}
if ($resultCode -ne 0) { throw "E2E tests failed with exit code $resultCode" }
