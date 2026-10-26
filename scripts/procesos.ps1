# Qué está corriendo del conciliador y en qué puerto, para poder bajarlo.
# Uso:  powershell -ExecutionPolicy Bypass -File scripts\procesos.ps1
#       powershell -ExecutionPolicy Bypass -File scripts\procesos.ps1 -Matar 8766
#
# En esta PC conviven varias apps en Python (planes, parte diario, MCP...), así
# que NUNCA hay que matar "todos los python": acá se ve cuál es cuál.
param([int]$Matar = 0)

function Puertos($pid_) {
  (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.OwningProcess -eq $pid_ } |
    Select-Object -ExpandProperty LocalPort -Unique) -join ", "
}

$procs = Get-CimInstance Win32_Process -Filter "name='python.exe' or name='pythonw.exe'"
if (-not $procs) { "No hay ningún Python corriendo."; exit }

"PID    PUERTOS      ARRANCADO            COMANDO"
"-----  -----------  -------------------  ------------------------------------------------"
foreach ($p in $procs | Sort-Object CreationDate) {
  $puertos = Puertos $p.ProcessId
  $cmd = ($p.CommandLine -replace '\s+', ' ')
  if ($cmd.Length -gt 95) { $cmd = $cmd.Substring(0, 92) + "..." }
  "{0,-5}  {1,-11}  {2:yyyy-MM-dd HH:mm}  {3}" -f $p.ProcessId, $puertos, $p.CreationDate, $cmd
}

if ($Matar -gt 0) {
  $duenos = Get-NetTCPConnection -LocalPort $Matar -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
  if (-not $duenos) { ""; "Nadie está escuchando en el puerto $Matar."; exit }

  # uvicorn --reload son DOS procesos: el que vigila los archivos y el que
  # atiende, y el socket lo comparten. Matando solo al que figura como dueño
  # del puerto, el otro queda vivo y el puerto sigue ocupado (pasa siempre).
  # Por eso se baja la familia entera: padres y descendientes que sean Python.
  $todos = Get-CimInstance Win32_Process -Filter "name='python.exe' or name='pythonw.exe'"
  $familia = New-Object System.Collections.Generic.HashSet[int]
  foreach ($d in $duenos) {
    [void]$familia.Add([int]$d)
    # hacia arriba
    $p = $todos | Where-Object { $_.ProcessId -eq $d }
    while ($p) {
      $padre = $todos | Where-Object { $_.ProcessId -eq $p.ParentProcessId }
      if (-not $padre) { break }
      [void]$familia.Add([int]$padre.ProcessId)
      $p = $padre
    }
  }
  # hacia abajo (repetir hasta que no aparezcan nietos nuevos)
  for ($i = 0; $i -lt 5; $i++) {
    foreach ($h in $todos) {
      if ($familia.Contains([int]$h.ParentProcessId)) { [void]$familia.Add([int]$h.ProcessId) }
    }
  }

  ""
  foreach ($pid_ in $familia) {
    "Bajando PID $pid_"
    Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Seconds 2
  if (Get-NetTCPConnection -LocalPort $Matar -State Listen -ErrorAction SilentlyContinue) {
    "Ojo: el puerto $Matar sigue ocupado. Volvé a correr el script para ver quién quedó."
  } else {
    "Puerto $Matar libre. Ahora sí, levantalo de nuevo (iniciar_8766.bat)."
  }
}
