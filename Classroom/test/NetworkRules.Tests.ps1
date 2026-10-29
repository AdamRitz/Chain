# Unit/integration boundary test: all firewall cmdlets below are mocks. No
# administrator privileges or persistent firewall changes are made by this test.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/NetworkRules.ps1')
$script:rules = @()
$script:changes = @()
$script:deny = $false
function Assert($condition, [string]$message) { if (-not $condition) { throw $message } }
function Get-NetFirewallRule { param($PolicyStore, $ErrorAction) if ($script:deny) { throw 'Policy access denied' }; $script:rules }
function Get-NetFirewallApplicationFilter { param([Parameter(ValueFromPipeline)]$InputObject) process { [pscustomobject]@{Program=$InputObject.Program} } }
function Get-NetFirewallPortFilter { param([Parameter(ValueFromPipeline)]$InputObject) process { [pscustomobject]@{Protocol=$InputObject.Protocol;LocalPort=$InputObject.LocalPort} } }
function Get-NetFirewallAddressFilter { param([Parameter(ValueFromPipeline)]$InputObject) process { [pscustomobject]@{RemoteAddress=$InputObject.RemoteAddress} } }
function Get-NetConnectionProfile { param($ErrorAction) [pscustomobject]@{InterfaceAlias='Classroom';NetworkCategory='Public';IPv4Connectivity='LocalNetwork';IPv6Connectivity='Internet'} }
function Get-NetFirewallProfile { param($PolicyStore,$ErrorAction) [pscustomobject]@{Name='Public';Enabled='True';AllowLocalFirewallRules='False';AllowInboundRules='True';DefaultInboundAction='Block'} }
function Get-NetIPAddress { param($ErrorAction) [pscustomobject]@{AddressState='Preferred';IPAddress='2001:db8::2';PrefixLength=64;AddressFamily='IPv6';InterfaceAlias='Classroom'} }
function New-NetFirewallRule { param($Name,$DisplayName,$Group,$Description,$PolicyStore,$Profile,$Direction,$Action,$Enabled,$Program,$Protocol,$LocalPort,$RemotePort,$LocalAddress,$RemoteAddress,$EdgeTraversalPolicy,$ErrorAction)
    Assert ($PolicyStore -eq 'PersistentStore' -and $Direction -eq 'Inbound' -and $Protocol -eq 'TCP' -and $EdgeTraversalPolicy -eq 'Block') 'Scope unexpectedly broadened'
    $script:rules += [pscustomobject]@{Name=$Name;Group=$Group;Program=$Program;Profile=$Profile;Direction=$Direction;Action=$Action;Enabled=$Enabled;Protocol=$Protocol;LocalPort=$LocalPort;RemoteAddress=$RemoteAddress}
    $script:changes += 'create'
}
function Set-NetFirewallRule { param($Name,$PolicyStore,$Profile,$Direction,$Action,$Enabled,$Program,$Protocol,$LocalPort,$RemotePort,$LocalAddress,$RemoteAddress,$EdgeTraversalPolicy,$ErrorAction)
    $r=$script:rules | Where-Object Name -eq $Name
    $r.Profile=$Profile; $r.RemoteAddress=$RemoteAddress; $script:changes += 'update'
}
function Remove-NetFirewallRule { param($PolicyStore,$Name,$ErrorAction) $script:rules=@($script:rules | Where-Object Name -ne $Name);$script:changes += 'remove' }
function Test-Path { param($LiteralPath,$PathType) $true }
$node='C:\Norn 测试\runtime\boost.exe'; $other='C:\Other installation\runtime\boost.exe'
Assert ((Get-NornRuleIdentity $node) -ne (Get-NornRuleIdentity $other)) 'Installations overlap'
foreach ($remote in @('Any','*','::/0','0.0.0.0/0','1.2.3.4;exit','127.1','::ffff:127.0.0.1','1.2.3.4/33')) {
    $rejected=$false;try {Assert-NornScope 'Private' $remote} catch {$rejected=$true}; Assert $rejected "Accepted $remote"
}
Set-NornNetworkRule $node 'Private' 'LocalSubnet'
Set-NornNetworkRule $node 'Public' '192.168.1.0/24,2001:db8::/64'
Set-NornNetworkRule $other 'Domain' '10.1.0.0/16'
Assert ($script:rules.Count -eq 2) 'Duplicate rule created'
$status=Get-NornNetworkStatus $node
Assert ($status.rules.Count -eq 1 -and $status.rules[0].profile -eq 'Public' -and $status.addresses[0].family -eq 'IPv6') 'Status lost scope or IPv6'
Assert ($status.policy[0].allow_local_rules -eq 'False') 'Managed policy hidden'
Remove-NornNetworkRule $node
Assert ($script:rules.Count -eq 1 -and $script:rules[0].Program -eq $other) 'Removed unrelated installation rule'
Remove-NornNetworkRule $node
Assert ($script:changes.Count -eq 4) 'No-op remove mutated firewall'
$script:deny=$true
$failed=$false;try {Get-NornNetworkStatus $node} catch {$failed=$true}; Assert $failed 'Policy read failure mistaken for empty rule'
$script:deny=$false
$script:rules[0].Name=Get-NornRuleIdentity $node
$failed=$false;try {Remove-NornNetworkRule $node} catch {$failed=$true};Assert $failed 'Ownership collision was removed'
@{passed=8;checks=@('bounded dual-stack scopes','path-specific identity','create/update without duplicate','IPv6 and managed policy reporting','remove only own installation','no-op remove','policy failure surfaces','ownership collision rejected');real_firewall_changed=$false} | ConvertTo-Json -Depth 4
