param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$projectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$phoneDir = Join-Path $projectDir '.env.phone'
$openssl = 'C:\Program Files\Git\usr\bin\openssl.exe'
if (-not (Test-Path -LiteralPath $openssl)) { throw 'Git for Windows OpenSSL is required.' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Node.js is required for the local HTTPS proxy.' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker is required.' }

$connection = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up' } | Select-Object -First 1
if (-not $connection) { throw 'No active Wi-Fi/LAN connection with a default gateway was found.' }
$phoneIp = $connection.IPv4Address[0].IPAddress
$phonePort = 3443
$origin = "https://${phoneIp}:${phonePort}"
New-Item -ItemType Directory -Force -Path $phoneDir | Out-Null

$ipFile = Join-Path $phoneDir 'ip.txt'
$serverCert = Join-Path $phoneDir 'server.crt'
$serverKey = Join-Path $phoneDir 'server.key'
$caCert = Join-Path $phoneDir 'ca.crt'
$caDer = Join-Path $phoneDir 'ca.der'
$needsCertificate = -not (Test-Path $serverCert) -or -not (Test-Path $serverKey) -or -not (Test-Path $caDer) -or -not (Test-Path $ipFile) -or ((Get-Content $ipFile -Raw).Trim() -ne $phoneIp)
if (-not $needsCertificate) {
    & $openssl x509 -checkend 86400 -noout -in $serverCert *> $null
    $needsCertificate = $LASTEXITCODE -ne 0
}
if ($needsCertificate) {
    $caKey = Join-Path $phoneDir 'ca.key'
    $serverRequest = Join-Path $phoneDir 'server.csr'
    $extensions = Join-Path $phoneDir 'server.ext'
    & $openssl req -x509 -newkey rsa:3072 -noenc -sha256 -days 30 -keyout $caKey -out $caCert -subj '/CN=FACEMARK Local iPhone CA' -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign'
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the local CA certificate.' }
    & $openssl req -new -newkey rsa:2048 -noenc -keyout $serverKey -out $serverRequest -subj "/CN=$phoneIp"
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the HTTPS server key.' }
    [System.IO.File]::WriteAllText($extensions, "subjectAltName=IP:$phoneIp`nbasicConstraints=critical,CA:FALSE`nkeyUsage=critical,digitalSignature,keyEncipherment`nextendedKeyUsage=serverAuth`n")
    & $openssl x509 -req -in $serverRequest -CA $caCert -CAkey $caKey -CAcreateserial -out $serverCert -days 30 -sha256 -extfile $extensions
    if ($LASTEXITCODE -ne 0) { throw 'Could not sign the HTTPS certificate.' }
    & $openssl x509 -in $caCert -outform DER -out $caDer
    if ($LASTEXITCODE -ne 0) { throw 'Could not export the iPhone trust certificate.' }
    [System.IO.File]::WriteAllText($ipFile, $phoneIp)
    Remove-Item -LiteralPath $caKey, $serverRequest, $extensions -ErrorAction SilentlyContinue
}

$certData = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($caDer))
$profileId = [guid]::NewGuid().ToString().ToUpperInvariant()
$certId = [guid]::NewGuid().ToString().ToUpperInvariant()
$profile = @"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>PayloadContent</key><array><dict>
<key>PayloadCertificateFileName</key><string>FACEMARK Local iPhone CA</string>
<key>PayloadContent</key><data>$certData</data>
<key>PayloadDescription</key><string>Trusts this computer's local FACEMARK HTTPS address.</string>
<key>PayloadDisplayName</key><string>FACEMARK Local Certificate</string>
<key>PayloadIdentifier</key><string>az.facemark.local.certificate</string>
<key>PayloadType</key><string>com.apple.security.root</string>
<key>PayloadUUID</key><string>$certId</string>
<key>PayloadVersion</key><integer>1</integer>
</dict></array>
<key>PayloadDisplayName</key><string>FACEMARK Local HTTPS</string>
<key>PayloadIdentifier</key><string>az.facemark.local.https</string>
<key>PayloadRemovalDisallowed</key><false/>
<key>PayloadType</key><string>Configuration</string>
<key>PayloadUUID</key><string>$profileId</string>
<key>PayloadVersion</key><integer>1</integer>
</dict></plist>
"@
[System.IO.File]::WriteAllText((Join-Path $projectDir 'frontend/public/phone-ca.mobileconfig'), $profile)

$envPath = Join-Path $projectDir '.env'
$envText = [System.IO.File]::ReadAllText($envPath)
if ($envText -notmatch '(?m)^APP_ORIGIN=') { throw 'APP_ORIGIN is missing from .env.' }
$envText = [regex]::Replace($envText, '(?m)^APP_ORIGIN=.*$', "APP_ORIGIN=$origin")
[System.IO.File]::WriteAllText($envPath, $envText)

Push-Location $projectDir
try {
    & docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose could not start FACEMARK.' }

    $existing = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -like '*phone-https-proxy.mjs*' }
    foreach ($process in $existing) { Stop-Process -Id $process.ProcessId -Force }
    $env:FACEMARK_PHONE_IP = $phoneIp
    $env:FACEMARK_PHONE_PORT = [string]$phonePort
    $proxy = Start-Process -FilePath (Get-Command node).Source -ArgumentList (Join-Path $projectDir 'scripts/phone-https-proxy.mjs') -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru
    $proxyReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $pending = $client.BeginConnect($phoneIp, $phonePort, $null, $null)
            if ($pending.AsyncWaitHandle.WaitOne(2000)) {
                $client.EndConnect($pending)
                $proxyReady = $true
                break
            }
        } catch {
            # The proxy is still starting.
        } finally {
            $client.Dispose()
        }
    }
    if (-not $proxyReady) { throw 'Local HTTPS proxy did not become ready.' }

    Write-Host "FACEMARK iPhone URL: $origin/"
    Write-Host "iPhone certificate profile: http://${phoneIp}:3000/phone-ca.mobileconfig"
    Write-Host "HTTPS proxy PID: $($proxy.Id)"
    if (-not $NoBrowser) { Start-Process $origin }
} finally {
    Pop-Location
}