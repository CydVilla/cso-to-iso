# Signing the Windows build

Windows shows a *Windows protected your PC* dialog for programs it has no
reputation record for. Reputation is tied to a code signing identity, so the
only way to remove the dialog is to sign the executable. This describes how to
turn signing on for this repository.

Until the secrets below exist, [the release workflow](workflows/release.yml)
skips both signing steps and publishes unsigned builds exactly as it does now.
Nothing breaks while the Azure side is still being set up.

## What this costs and how long it takes

Microsoft's service was called Azure Trusted Signing until January 2026 and is
now **Artifact Signing**. The basic tier runs at roughly ten dollars a month
with a monthly signature allowance far beyond what this project needs. Check
the current pricing page before committing, as the tiers change.

The slow part is identity validation, not the setup. Microsoft verifies who you
are before issuing a certificate profile. An individual account needs
government identification and around three years of verifiable public record
tied to your name. Approval usually takes a few business days.

## One-time setup in Azure

1. **Create a subscription.** Pay-as-you-go is fine. Note the subscription id.

2. **Register the resource provider.** In the subscription's *Resource
   providers* list, register `Microsoft.CodeSigning`.

3. **Create an Artifact Signing account.** Pick a region close to you and note
   its endpoint, which looks like `https://eus.codesigning.azure.net/`. The
   endpoint must match the region the account lives in.

4. **Complete identity validation.** Start this immediately, since everything
   else waits on it. Choose the individual path unless you are signing on
   behalf of a registered company.

5. **Create a certificate profile** once validation is approved. Choose the
   public trust type. Note the profile name.

6. **Create an app registration** in Microsoft Entra ID so the workflow can
   authenticate. Add a client secret and copy the value straight away, because
   it is only shown once. Note the application id and the directory id.

7. **Grant the app permission to sign.** On the Artifact Signing account, give
   the app registration the **Artifact Signing Certificate Profile Signer**
   role. Without this the workflow authenticates and then fails with a 403.

Microsoft's own walkthrough is at
<https://learn.microsoft.com/azure/trusted-signing/>.

## Secrets to add to this repository

Settings → Secrets and variables → Actions → New repository secret.

| Secret | Where it comes from |
| --- | --- |
| `AZURE_CLIENT_ID` | Application id of the app registration |
| `AZURE_CLIENT_SECRET` | The client secret value |
| `AZURE_TENANT_ID` | Directory id of the Entra tenant |
| `AZURE_SIGNING_ENDPOINT` | Account endpoint, for example `https://eus.codesigning.azure.net/` |
| `AZURE_SIGNING_ACCOUNT` | Artifact Signing account name |
| `AZURE_CERT_PROFILE` | Certificate profile name |

`AZURE_CLIENT_ID` is the switch. The workflow checks that one value and skips
signing when it is empty. The last three are not really sensitive, but keeping
all six in one screen means there is only one place to look.

## Turning it on

Push a tag as usual:

```bash
git tag -a v1.0.1 -m "cso2iso 1.0.1"
git push origin v1.0.1
```

The Windows job signs the executable, verifies the signature and refuses to
publish if it is anything but valid. The build log prints the signing subject.

To confirm on a real machine, right-click the downloaded `cso2iso.exe`, open
Properties and look for a *Digital Signatures* tab. From PowerShell:

```powershell
Get-AuthenticodeSignature .\cso2iso.exe | Format-List
```

## What to expect afterwards

The unknown publisher line disappears with the first signed release. Signed
programs from a brand new identity can still draw a SmartScreen prompt until
the identity accumulates some download history, though it clears far faster
than for an unsigned file and often not at all.

Once a signed release is out, drop the SmartScreen warning from the
[README](../README.md) and from the download page in [docs](../docs/index.html),
since it will no longer describe what people see.

## If signing fails

- **403 on the signing call.** The app registration is missing the certificate
  profile signer role, or it was granted on the wrong resource.
- **Endpoint or account not found.** The endpoint region does not match where
  the account was created.
- **Signature reported as `NotSigned` afterwards.** The build ran before the
  secrets were added, so the signing step was skipped.
- **Signature reported as `UnknownError`.** Usually a missing timestamp.
  Signatures without one expire in days, which is why the workflow always
  passes a timestamp server.
