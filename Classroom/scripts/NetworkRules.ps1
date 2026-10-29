# Embedded in norn-network.exe. This file defines functions only, allowing the
# policy to be tested with mocked Windows cmdlets without changing the firewall.
function Get-NornRuleIdentity([string]$NodePath) {
    $path = [IO.Path]::GetFullPath($NodePath).ToLowerInvariant()
    if ([IO.Path]::GetFileName($path) -ne 'boost.exe') { throw 'Invalid node executable' }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($path))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    return "NornV2.Node.$hash"
}

function Assert-NornScope([string]$Profile, [string]$Remote) {
    if ($Profile -cnotin @('Private', 'Domain', 'Public')) { throw 'Select Private, Domain or Public' }
    $entries = @($Remote.Split(','))
    if ($entries.Count -gt 16 -or $Remote.Length -gt 1024) { throw 'Too many remote addresses' }
    foreach ($entry in $entries) {
        if ($entry -ceq 'LocalSubnet') { continue }
        if ($entry -notmatch '^[0-9a-fA-F:.]+(/[0-9]{1,3})?$') { throw 'Use LocalSubnet or literal IP addresses/CIDR prefixes' }
        $parts = $entry.Split('/')
        $address = $null
        if (-not [Net.IPAddress]::TryParse($parts[0], [ref]$address)) { throw 'Invalid IP address' }
        $v4 = $address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork
        if ($v4 -and $parts[0] -notmatch '^(\d{1,3}\.){3}\d{1,3}$') { throw 'IPv4 needs four decimal octets' }
        if ($address.Equals([Net.IPAddress]::Any) -or $address.Equals([Net.IPAddress]::IPv6Any) -or $address.IsIPv4MappedToIPv6 -or $address.IsIPv6Multicast) { throw 'Invalid remote address' }
        if ($v4 -and ($address.GetAddressBytes()[0] -ge 224)) { throw 'Multicast/broadcast is not a classroom scope' }
        if ($parts.Count -eq 2 -and ([int]$parts[1] -lt 1 -or [int]$parts[1] -gt $(if ($v4) {32} else {128}))) { throw 'Invalid prefix; an Internet-wide /0 is not allowed' }
    }
}

function Get-NornOwnedRule([string]$NodePath, [string]$Store = 'PersistentStore') {
    $name = Get-NornRuleIdentity $NodePath
    # An empty result is distinct from a failed policy query.
    $rules = @(Get-NetFirewallRule -PolicyStore $Store -ErrorAction Stop | Where-Object Name -eq $name)
    foreach ($rule in $rules) {
        $program = ($rule | Get-NetFirewallApplicationFilter -ErrorAction Stop).Program
        if ($rule.Group -ne 'NornV2.Classroom.Network.v1' -or [IO.Path]::GetFullPath($program) -ine [IO.Path]::GetFullPath($NodePath)) {
            throw 'Rule ownership conflict; contact the administrator'
        }
    }
    return $rules
}

function Get-NornNetworkStatus([string]$NodePath) {
    $rules = @(Get-NornOwnedRule $NodePath)
    $active = @(Get-NornOwnedRule $NodePath 'ActiveStore')
    $details = @($rules | ForEach-Object {
        $filter = $_ | Get-NetFirewallPortFilter -ErrorAction Stop
        [ordered]@{name=$_.Name; enabled=[string]$_.Enabled; action=[string]$_.Action; direction=[string]$_.Direction;
            profile=[string]$_.Profile; protocol=[string]$filter.Protocol; local_port=@($filter.LocalPort);
            remote=@(($_ | Get-NetFirewallAddressFilter -ErrorAction Stop).RemoteAddress)}
    })
    $profiles = @(Get-NetConnectionProfile -ErrorAction Stop | ForEach-Object {
        [ordered]@{interface=$_.InterfaceAlias; category=[string]$_.NetworkCategory;
            ipv4=[string]$_.IPv4Connectivity; ipv6=[string]$_.IPv6Connectivity}
    })
    $policy = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop | ForEach-Object {
        [ordered]@{name=[string]$_.Name; enabled=[string]$_.Enabled; allow_local_rules=[string]$_.AllowLocalFirewallRules;
            allow_inbound=[string]$_.AllowInboundRules; default_inbound=[string]$_.DefaultInboundAction}
    })
    $addresses = @(Get-NetIPAddress -ErrorAction Stop | Where-Object {$_.AddressState -eq 'Preferred' -and $_.IPAddress -notin @('127.0.0.1','::1')} | ForEach-Object {
        [ordered]@{interface=$_.InterfaceAlias; address=$_.IPAddress; prefix=$_.PrefixLength; family=[string]$_.AddressFamily}
    })
    return [ordered]@{supported=$true; node=$NodePath; rule_name=(Get-NornRuleIdentity $NodePath); rules=$details;
        active_rule_count=$active.Count; profiles=$profiles; policy=$policy; addresses=$addresses}
}

function Set-NornNetworkRule([string]$NodePath, [string]$Profile, [string]$Remote) {
    Assert-NornScope $Profile $Remote
    if (-not (Test-Path -LiteralPath $NodePath -PathType Leaf)) { throw 'Node executable is missing' }
    $rules = @(Get-NornOwnedRule $NodePath)
    $parameters = @{PolicyStore='PersistentStore'; Profile=$Profile; Direction='Inbound'; Action='Allow'; Enabled='True';
        Program=$NodePath; Protocol='TCP'; LocalPort='Any'; RemotePort='Any'; LocalAddress='Any'; RemoteAddress=$Remote.Split(',');
        EdgeTraversalPolicy='Block'; ErrorAction='Stop'}
    if ($rules.Count) {
        Set-NetFirewallRule -Name (Get-NornRuleIdentity $NodePath) @parameters | Out-Null
    } else {
        New-NetFirewallRule -Name (Get-NornRuleIdentity $NodePath) -DisplayName 'NornV2 classroom node (TCP)' `
            -Group 'NornV2.Classroom.Network.v1' -Description 'NornV2 node only; local management gateway excluded.' @parameters | Out-Null
    }
}

function Remove-NornNetworkRule([string]$NodePath) {
    foreach ($rule in @(Get-NornOwnedRule $NodePath)) {
        Remove-NetFirewallRule -PolicyStore PersistentStore -Name $rule.Name -ErrorAction Stop
    }
}
