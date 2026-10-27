# Qué está corriendo del conciliador y en qué puerto, para poder bajarlo.
#
# Uso:  powershell -ExecutionPolicy Bypass -File scripts\procesos.ps1
#       powershell -ExecutionPolicy Bypass -File scripts\procesos.ps1 -Matar 8766
#       powershell -ExecutionPolicy Bypass -File scripts\procesos.ps1 -MatarPid 4724
#
# En esta PC conviven varias apps en Python (planes, parte diario, MCP...), así
# que NUNCA hay que matar "todos los python". Para saber cuál es cuál, a cada
# puerto que escucha se le pregunta quién es: las apps del grupo responden su
# nombre y su versión.
param([int]$Matar = 0, [int]$MatarPid = 0)

$ErrorActionPreference = "SilentlyContinue"

function Puertos($elPid) {
  (Get-NetTCPConnection -State Listen |
    Where-Object { $_.OwningProcess -eq $elPid } |
    Select-Object -ExpandProperty LocalPort -Unique | Sort-Object) -join ", "
}

function QuienEs($puerto) {
  # /api/version la tienen las apps del grupo; si no, el <title> de la home
  try {
    $v = Invoke-RestMethod "http://127.0.0.1:$puerto/api/version" -TimeoutSec 2
    if ($v.commit) {
      $falta = if ($v.falta_reiniciar) { " (EN EL DISCO YA HAY OTRA: $($v.en_disco))" } else { "" }
      return "version $($v.commit), desde $($v.arrancado)$falta"
    }
  } catch {}
  try {
    $r = Invoke-WebRequest "http://127.0.0.1:$puerto/" -TimeoutSec 2 -UseBasicParsing
    if ($r.Content -match "<title>([^<]{1,60})</title>") { return $Matches[1].Trim() }
    return "responde HTTP"
  } catch {}
  return ""
}

$procs = Get-CimInstance Win32_Process -Filter "name='python.exe' or name='pythonw.exe'"
if (-not $procs) { "No hay ningun Python corriendo."; exit }

$sinComando = $false
"PID    PUERTOS  ARRANCADO         QUE ES / COMANDO"
"-----  -------  ----------------  --------------------------------------------------------"
foreach ($p in $procs | Sort-Object CreationDate) {
  $puertos = Puertos $p.ProcessId
  $que = ""
  foreach ($puerto in ($puertos -split ",\s*" | Where-Object { $_ })) {
    $quien = QuienEs $puerto
    if ($quien) { $que = $quien; break }
  }
  if (-not $que) {
    if ($p.CommandLine) {
      $que = ($p.CommandLine -replace '\s+', ' ')
      if ($que.Length -gt 56) { $que = $que.Substring(0, 53) + "..." }
    } else {
      $sinComando = $true
      $que = "(sin puerto; probablemente el companiero del de arriba)"
    }
  }
  "{0,-5}  {1,-7}  {2:yyyy-MM-dd HH:mm}  {3}" -f $p.ProcessId, $puertos, $p.CreationDate, $que
}
if ($sinComando) {
  ""
  "Nota: los procesos sin puerto no se pueden identificar desde esta consola."
  "      uvicorn --reload son DOS procesos (el que vigila y el que atiende) y"
  "      arrancan juntos: mira la hora, el que acompania al del puerto es su par."
}

function BajarFamilia($objetivos) {
  $todos = Get-CimInstance Win32_Process -Filter "name='python.exe' or name='pythonw.exe'"
  $familia = New-Object System.Collections.Generic.HashSet[int]
  foreach ($d in $objetivos) {
    [void]$familia.Add([int]$d)
    $p = $todos | Where-Object { $_.ProcessId -eq $d }
    while ($p) {    # hacia arriba, mientras el padre siga siendo Python
      $padre = $todos | Where-Object { $_.ProcessId -eq $p.ParentProcessId }
      if (-not $padre) { break }
      [void]$familia.Add([int]$padre.ProcessId)
      $p = $padre
    }
  }
  for ($i = 0; $i -lt 5; $i++) {    # y hacia abajo, hijos y nietos
    foreach ($h in $todos) {
      if ($familia.Contains([int]$h.ParentProcessId)) { [void]$familia.Add([int]$h.ProcessId) }
    }
  }
  # Write-Host y no texto suelto: lo que la funcion escribe en la salida se lo
  # lleva el [void](...) de quien la llama, junto con el valor de retorno, y
  # entonces no se ve cual bajo y cual no.
  Write-Host ""
  $fallaron = @()
  foreach ($elPid in $familia) {
    Stop-Process -Id $elPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
    if (Get-Process -Id $elPid -ErrorAction SilentlyContinue) {
      Write-Host "PID $elPid NO se pudo bajar"
      $fallaron += $elPid
    } else {
      Write-Host "PID $elPid bajado"
    }
  }
  if ($fallaron) {
    Write-Host ""
    Write-Host "Esos procesos son de otro usuario o sesion: abri PowerShell como"
    Write-Host "administrador (boton derecho > Ejecutar como administrador) y repeti."
  }
  return $fallaron
}

if ($MatarPid -gt 0) { [void](BajarFamilia @($MatarPid)) }

if ($Matar -gt 0) {
  # uvicorn --reload comparte el socket entre los dos procesos: matando solo al
  # que figura como dueno del puerto, el otro queda vivo con el puerto tomado y
  # el codigo VIEJO en memoria. Por eso se baja la familia entera.
  $duenos = Get-NetTCPConnection -LocalPort $Matar -State Listen |
    Select-Object -ExpandProperty OwningProcess -Unique
  if (-not $duenos) { ""; "Nadie esta escuchando en el puerto $Matar."; exit }
  [void](BajarFamilia $duenos)
  Start-Sleep -Seconds 2
  ""
  if (Get-NetTCPConnection -LocalPort $Matar -State Listen) {
    $quedan = (Get-NetTCPConnection -LocalPort $Matar -State Listen |
      Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    "Ojo: el puerto $Matar sigue ocupado por el PID $quedan."
    "Bajalo directo:  scripts\procesos.ps1 -MatarPid $quedan"
  } else {
    "Puerto $Matar libre. Ahora si, levantalo de nuevo (iniciar_8766.bat)."
  }
}
