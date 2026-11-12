<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/keepstack-logo-white.svg">
    <img src="../assets/keepstack-logo.svg" width="380" alt="keepstack">
  </picture>
</p>

<p align="center">
  <strong>Estado persistente para AWS Emulator.</strong><br>
  Tu infraestructura local de AWS sobrevive a los reinicios: <code>terraform plan</code> sigue diciendo <em>No changes</em>.
</p>

<p align="center">
  <a href="https://github.com/sanlega/keepstack/actions/workflows/ci.yml"><img src="https://github.com/sanlega/keepstack/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="../../LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen.svg" alt="No dependencies">
</p>

<p align="center">
  <a href="../../README.md">English</a> · <strong>Español</strong> · <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center"><em>Si algo difiere de esta traducción, la versión de referencia es el README en inglés.</em></p>

---

## Por qué

[AWS Emulator](https://docs.aws.amazon.com/emulator/latest/userguide/what-is-service.html) es el emulador local oficial
de las APIs de AWS. Es rápido y fiel, pero lo guarda todo **en memoria**: si lo paras, reinicias Docker o apagas el
portátil, desaparecen todas las VPC, buckets y tablas. Tu estado de Terraform sigue listándolos, así que el siguiente
`terraform plan` quiere recrear toda tu infraestructura en lugar de mostrarte el único cambio que has hecho.

**keepstack** lo soluciona. Se coloca delante del emulador, registra cada escritura correcta en un pequeño journal
SQLite y la reproduce cada vez que arranca el emulador. El trabajo real con infraestructura (déjala días, vuelve,
añade un servicio) ahora se comporta como en AWS.

```console
$ terraform plan            # after a full reboot
No changes. Your infrastructure matches the configuration.

$ terraform plan            # after adding one subnet
Plan: 1 to add, 0 to change, 0 to destroy.
```

## Características

- **Sobrevive a todo**: Ctrl+C, `docker kill`, reinicios de Docker, reinicios del equipo. Si el contenedor muere
  mientras trabajas, keepstack lo levanta de nuevo y restaura el estado por su cuenta.
- **IDs estables**: los valores aleatorios que genera el servidor (`vpc-…`, UUID de claves KMS, sufijos de ARN de
  Secrets Manager, IDs de zona de Route 53, receipt handles de SQS…) cambian en cada replay. keepstack aprende la
  correspondencia y traduce en ambos sentidos, de modo que tus herramientas siempre ven los valores originales.
- **Cero configuración**: un solo comando. La imagen del emulador se construye sola la primera vez (x86_64 o Apple silicon).
- **Seguro por diseño**: las escrituras se registran *antes* de llegar al emulador, hay una instancia por puerto y un
  segundo `keepstack up --reset` nunca puede borrar el estado de una instancia en ejecución.
- **Sin dependencias**: solo la biblioteca estándar de Python. Funciona en macOS, Linux y Windows.
- **Probado con Terraform real**: más de 40 tipos de recurso de redes, serverless y servicios de datos.
  Consulta la [matriz de compatibilidad](../compatibility.md).

## Instalación

Requisitos: [Docker](https://docs.docker.com/get-docker/) y Python 3.10+.

```sh
pipx install git+https://github.com/sanlega/keepstack
# or: uv tool install git+https://github.com/sanlega/keepstack
```

## Inicio rápido

```sh
keepstack up                 # terminal 1 — restaura el estado y sirve la API de AWS en http://127.0.0.1:9297
```

```sh
eval "$(keepstack env)"      # terminal 2 — apunta AWS CLI, los SDK y Terraform a keepstack
                             # PowerShell: keepstack env --shell powershell | Invoke-Expression
aws sts get-caller-identity  # "Account": "000000000000" — estás hablando con el emulador
```

Prueba el ciclo completo con el ejemplo incluido:

```sh
cd examples/quickstart
terraform init && terraform apply -auto-approve
# para keepstack (Ctrl+C) —o reinicia el equipo— y arráncalo de nuevo: keepstack up
terraform plan                       # No changes
terraform plan -var extra=true       # Plan: 1 to add — solo el cambio nuevo
```

El estado vive en `.keepstack/`, en el directorio donde ejecutas `keepstack up`, igual que `.terraform/`: un estado por
proyecto. Usa `keepstack up --reset` para empezar de cero.

> [!IMPORTANT]
> Usa **AWS CLI v2**. AWS CLI v1 ignora `AWS_ENDPOINT_URL` en algunos servicios (p. ej. SQS) y enviaría esas llamadas
> a AWS de verdad. En Terraform, usa el bloque `provider` de [`examples/quickstart/main.tf`](../../examples/quickstart/main.tf)
> (configura `s3_use_path_style = true`).

## Cómo funciona

```
Terraform / AWS CLI / SDKs ──http──► :9297 keepstack ──https──► :9298 AWS Emulator (Docker)
                                          │
                                          └── .keepstack/journal.db   (SQLite)
```

1. **Registro**: cada llamada de escritura (`Create*`, `Put*`, `PUT` de S3…) se anota en el journal antes de
   reenviarse; las lecturas pasan directamente.
2. **Replay**: al arrancar, keepstack lanza un emulador limpio y reproduce el journal en orden, reintentando las
   llamadas que se adelantan al emulador (p. ej. `PutItem` mientras la tabla sigue en `CREATING`).
3. **Traducción**: compara cada respuesta reproducida con la registrada, aprende qué valores generados han cambiado
   y los reescribe en ambos sentidos.

Lee el diseño completo en [docs/how-it-works.md](../how-it-works.md).

## Uso

```text
keepstack up   [--port 9297] [--emulator-port 9298] [--data-dir .keepstack] [--image keepstack-emulator]
               [--reset] [--verbose]
keepstack env  [--port 9297] [--shell sh|powershell]
keepstack --version
```

| Opción | Por defecto | Descripción |
|---|---|---|
| `--port` | `9297` | Puerto al que se conectan tus herramientas (HTTP plano en 127.0.0.1). |
| `--emulator-port` | `9298` | Puerto interno del contenedor del emulador. |
| `--data-dir` | `.keepstack` | Dónde se guarda el journal. |
| `--image` | `keepstack-emulator` | Imagen Docker del emulador. Se construye automáticamente si es la de por defecto y no existe. |
| `--reset` | — | Borra el estado guardado y empieza de cero. |
| `--verbose` | — | Registra cada petición en el log. |

Puedes ejecutar varios entornos aislados en paralelo dándole a cada uno su propio `--port`, `--emulator-port` y `--data-dir`.

## Limitaciones

- **Solo para desarrollo local.** keepstack no comprueba credenciales y ejecuta el emulador con `--auth off`. Solo
  escucha en `127.0.0.1`. Consulta [SECURITY.md](../../SECURITY.md).
- **No es AWS.** Que algo pase en local no garantiza que funcione en AWS: el emulador no evalúa políticas IAM y su
  fidelidad varía según el servicio. Las carencias conocidas del emulador están en la [matriz de compatibilidad](../compatibility.md).
- **Traducción de IDs heurística.** Los valores solo se aprenden si parecen identificadores generados; consulta
  [cómo funciona](../how-it-works.md#limitations) para ver las reglas exactas y los casos límite.
- **El journal solo crece.** Para compactarlo, ejecuta `keepstack up --reset` y vuelve a lanzar `terraform apply`.

## Contribuir

Las issues y las pull requests son bienvenidas, sobre todo nuevas entradas para la [matriz de compatibilidad](../compatibility.md).
Consulta [CONTRIBUTING.md](../../CONTRIBUTING.md) para el entorno de desarrollo y la batería de tests.

## Licencia

[Apache-2.0](../../LICENSE).

keepstack es un proyecto independiente. No está afiliado a Amazon Web Services, ni cuenta con su respaldo ni patrocinio.
AWS y AWS Emulator son marcas comerciales de Amazon.com, Inc. o sus filiales.
