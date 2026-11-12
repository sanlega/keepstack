<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/keepstack-logo-white.svg">
    <img src="../assets/keepstack-logo.svg" width="360" alt="keepstack">
  </picture>
</p>

<h3 align="center">Tu AWS local sobrevive a los reinicios.</h3>

<p align="center">
  Estado persistente para <a href="https://docs.aws.amazon.com/emulator/latest/userguide/what-is-service.html">AWS Emulator</a>:
  reinicia, vuelve días después y <code>terraform plan</code> sigue diciendo <em>No changes</em>.
</p>

<p align="center">
  <a href="https://github.com/sanlega/keepstack/actions/workflows/ci.yml"><img src="https://github.com/sanlega/keepstack/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="../../LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen.svg" alt="Zero dependencies">
</p>

<p align="center">
  <a href="#inicio-rápido">Inicio rápido</a> ·
  <a href="../how-it-works.md">Cómo funciona</a> ·
  <a href="../compatibility.md">Compatibilidad</a> ·
  <a href="../../CONTRIBUTING.md">Contribuir</a>
  <br>
  <sub><a href="../../README.md">English</a> · <strong>Español</strong> · <a href="README.zh-CN.md">简体中文</a></sub>
</p>

<p align="center"><em>Si algo difiere de esta traducción, la versión de referencia es el README en inglés.</em></p>

<br>

## El problema

AWS Emulator es el emulador local oficial de AWS: rápido y fiel, pero **todo vive en memoria**.

<table>
<tr>
<th width="50%">😩 Sin keepstack</th>
<th width="50%">😌 Con keepstack</th>
</tr>
<tr>
<td>

```console
$ terraform apply      # 30 resources
$ # ...reboot...
$ terraform plan
Plan: 30 to add, 0 to change, 0 to destroy.
```

Todo ha desaparecido. Terraform quiere reconstruir
toda la pila y no puedes ver el único cambio que hiciste.

</td>
<td>

```console
$ terraform apply      # 30 resources
$ # ...reboot...
$ terraform plan
No changes. Your infrastructure matches
the configuration.
```

Mismas VPC, mismos IDs, mismos datos. Añade una subred y
`plan` muestra exactamente **1 to add**.

</td>
</tr>
</table>

## Inicio rápido

> **Requisitos:** [Docker](https://docs.docker.com/get-docker/) y Python 3.10+.

**1. Instala**

```sh
pipx install git+https://github.com/sanlega/keepstack
```

**2. Arráncalo:** la imagen del emulador se construye la primera vez y después se restaura tu estado guardado

```sh
keepstack up -d                # en segundo plano; o `keepstack up` para mantenerlo en esta terminal
```

**3. Apunta tus herramientas a keepstack**

```sh
eval "$(keepstack env)"        # PowerShell: keepstack env --shell powershell | iex
aws sts get-caller-identity    # "Account": "000000000000" → estás en el emulador
```

Eso es todo. Usa Terraform, la AWS CLI o cualquier SDK como siempre, y `keepstack down` cuando quieras.
Prueba el ciclo completo con [`examples/quickstart`](../../examples/quickstart/main.tf).

> [!IMPORTANT]
> Usa **AWS CLI v2**: la v1 ignora `AWS_ENDPOINT_URL` en algunos servicios (p. ej. SQS) y llamaría a AWS de verdad.
> En Terraform, copia el bloque `provider` del [ejemplo quickstart](../../examples/quickstart/main.tf).

## Comandos del día a día

| Comando | Qué hace |
|---|---|
| `keepstack check` | **Antes de aplicar:** lista qué recursos de tu proyecto Terraform funcionarán en el emulador, cuáles se sabe que fallan (con soluciones alternativas) y las carencias conocidas del emulador en cada servicio que usas. Termina con código 1 si algo va a fallar, así que encaja en CI. |
| `keepstack up -d` / `down` | Ejecuta en segundo plano / lo detiene. El estado se conserva en ambos casos. |
| `keepstack status` | ¿Está en marcha? Cuántas llamadas hay guardadas, por servicio, y cómo fue la última restauración. |
| `keepstack logs -f` | Sigue el registro en segundo plano. |
| `keepstack doctor` | Comprueba Docker, el puerto, la versión de tu AWS CLI y avisa si unas credenciales reales de AWS podrían filtrarse a tu sesión. |
| `keepstack snapshot save <name>` | Guarda el estado actual (añade `--include terraform.tfstate` para guardar contigo tu estado de Terraform). También `snapshot list`, `restore`, `delete`. |
| `keepstack export` / `import` | Comparte un entorno completo como un único archivo. |
| `keepstack compact` | Reduce el journal eliminando los recursos que se crearon y luego se borraron. Se verifica en un emulador desechable antes de aplicarlo. |
| `keepstack env` | Imprime las variables que apuntan tus herramientas a keepstack. |

```console
$ keepstack check examples/quickstart
keepstack check: examples/quickstart (scanned .tf files), emulator 1.0.322

⚠ works with a caveat
    aws_dynamodb_table          x1  PAY_PER_REQUEST shows a perpetual diff (OnDemandThroughput returns -1)
                                workaround: keepstack up --shims, or lifecycle { ignore_changes = [on_demand_throughput] }

✓ ok
    aws_kms_key                 x1  key UUID translated by keepstack
    aws_route53_zone            x1  zone ID translated by keepstack
    aws_subnet                  x2
    aws_vpc                     x1
    ...

14 resources: 13 ok, 1 with caveats, 0 broken, 0 untested.
```

Los snapshots hacen barato probar cambios arriesgados:

```sh
keepstack snapshot save before-upgrade --include terraform.tfstate
terraform apply                                     # ...¿no ha ido bien?
keepstack down && keepstack snapshot restore before-upgrade && keepstack up -d
```

## Características

| | |
|---|---|
| 🔁 **Sobrevive a todo** | Ctrl+C, `docker kill`, reinicios de Docker, reinicios del equipo. Si el contenedor muere, keepstack lo restaura por su cuenta. |
| 🪪 **IDs estables** | `vpc-…`, UUID de KMS, ARN de Secrets Manager, IDs de zona de Route 53… conservan su valor original tras los reinicios. |
| 🔍 **Sabes antes de aplicar** | `keepstack check` te dice qué fallará en el emulador antes de ejecutar Terraform. |
| 📸 **Snapshots** | Guarda, restaura y comparte entornos completos, estado de Terraform incluido. |
| 🩹 **Shims de compatibilidad** | `--shims` esquiva errores conocidos del emulador (p. ej. `aws_security_group`), desactivado por defecto. |
| ⚡ **Cero configuración** | Un solo comando, sin config. La imagen del emulador se construye sola (x86_64 y Apple silicon). |
| 🛡️ **Seguro por diseño** | Journal con escritura previa y una instancia por puerto: un `--reset` accidental no puede borrar un estado en ejecución. |
| 📦 **Sin dependencias** | Solo la biblioteca estándar de Python. macOS, Linux y Windows. |
| ✅ **Probado de verdad** | Más de 40 tipos de recurso de Terraform, de extremo a extremo contra un emulador real. [Ver la matriz →](../compatibility.md) |

## Cómo funciona

```mermaid
flowchart LR
    T["Terraform<br/>AWS CLI · SDKs"] -- "http :9297" --> K["keepstack"]
    K -- "https :9298" --> E["AWS Emulator<br/>(Docker)"]
    K <--> J[("journal.db<br/>SQLite")]
```

1. **Registro**: cada escritura se guarda en el journal *antes* de llegar al emulador. Las lecturas pasan directamente.
2. **Replay**: al arrancar, un emulador limpio recibe el journal en orden (con reintentos para los recursos que aún se están asentando).
3. **Traducción**: los nuevos IDs aleatorios se asignan de vuelta a los originales, así que tus herramientas nunca notan el reinicio.

El estado vive en `.keepstack/`, junto al directorio donde lo ejecutas: un estado por proyecto, como `.terraform/`.
El diseño completo está en [docs/how-it-works.md](../how-it-works.md).

## Referencia

<details>
<summary><strong>Comandos y opciones</strong></summary>

<br>

```text
keepstack up        [-d] [--port 9297] [--emulator-port 9298] [--data-dir .keepstack]
                    [--image keepstack-emulator] [--reset] [--shims] [--verbose]
keepstack down      [--data-dir]
keepstack status    [--data-dir] [--json]
keepstack logs      [--data-dir] [-f]
keepstack env       [--port 9297] [--shell sh|powershell]
keepstack check     [PATH] [--plan plan.json] [--offline] [--json]
keepstack doctor    [--port] [--data-dir]
keepstack snapshot  save NAME [--include FILE ...] [--force] | list | restore NAME | delete NAME
keepstack export    FILE
keepstack import    FILE
keepstack compact   [--dry-run]
keepstack --version
```

Todos los comandos que trabajan sobre un estado aceptan `--data-dir` (por defecto `.keepstack`). `restore`, `import` y
`compact` necesitan que keepstack esté parado y guardan antes el estado anterior como snapshot `pre-restore` / `pre-compact`,
para poder deshacerlos siempre.

| Opción | Por defecto | Descripción |
|---|---|---|
| `--port` | `9297` | Puerto al que se conectan tus herramientas (HTTP plano en 127.0.0.1) |
| `--emulator-port` | `9298` | Puerto interno del contenedor del emulador |
| `--data-dir` | `.keepstack` | Dónde se guarda el journal |
| `--image` | `keepstack-emulator` | Imagen del emulador; se construye automáticamente si es la de por defecto y no existe |
| `--reset` | | Borra el estado guardado y empieza de cero |
| `-d`, `--detach` | | Ejecuta en segundo plano; los registros van a `.keepstack/keepstack.log` |
| `--shims` | | Esquiva errores conocidos del emulador (consulta [cómo funciona](../how-it-works.md#compatibility-shims)); se recuerda para ese estado |
| `--verbose` | | Registra cada petición |

Puedes ejecutar varios entornos aislados en paralelo con distintos `--port`, `--emulator-port` y `--data-dir`.

</details>

<details>
<summary><strong>Limitaciones</strong></summary>

<br>

- **Solo para desarrollo local**: no hay comprobación de credenciales, el emulador se ejecuta con `--auth off` y escucha solo en `127.0.0.1`.
  Consulta [SECURITY.md](../../SECURITY.md).
- **No es AWS**: el emulador no evalúa IAM y su fidelidad varía según el servicio.
  Las carencias conocidas están en la [matriz de compatibilidad](../compatibility.md).
- **Traducción de IDs heurística**: solo se aprenden los valores que parecen generados.
  Detalles en [cómo funciona](../how-it-works.md#limitations).
- **El journal solo crece**: compáctalo con `keepstack up --reset` y un `terraform apply` nuevo.

</details>

## Contribuir

Las issues y las PR son bienvenidas, sobre todo nuevas entradas para la [matriz de compatibilidad](../compatibility.md).
Empieza por [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Licencia

[Apache-2.0](../../LICENSE)

<sub>keepstack es un proyecto independiente, sin afiliación, respaldo ni patrocinio de Amazon Web Services.
AWS y AWS Emulator son marcas comerciales de Amazon.com, Inc. o sus filiales.</sub>
