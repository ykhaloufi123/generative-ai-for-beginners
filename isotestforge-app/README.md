# ISO-TestForge

Génération automatisée de campagnes de test **ISO 8583** pour **JMeter** (plugin
[jmeter-iso8583](https://github.com/tilln/jmeter-iso8583)), à partir de besoins écrits en langage naturel.

Le testeur fournit des besoins (texte, Word, PDF, Excel, Jira). ISO-TestForge produit un plan `.jmx` complet
avec profils carte, terminaux, clés et cryptogrammes, l'exécute (JMeter ou runner natif) contre un simulateur
ou une plateforme d'homologation, puis livre un rapport avec diagnostics et couverture.

**Principe de coût : le déterministe d'abord.** L'IA ne produit jamais de JMX. Elle traduit des besoins en un
DSL validé, et seulement quand les règles ne suffisent pas. Sur l'exemple fourni, 100 % des besoins sont traités
sans aucun token.

---

## Démarrage rapide

```bash
pip install -r requirements.txt && pip install -e .

itf simulator --port 8583 &                                  # simulateur hôte (PIN, MAC, ARQC, CVV réellement vérifiés)
itf pipeline examples/requirements/acquisition_tpe.md \
    --name RECETTE_TPE --host SWITCH_SIM \
    --execute native --exec-host 127.0.0.1 --exec-port 8583  # besoins -> JMX -> exécution -> rapport
itf serve --port 8080                                        # interface web : http://localhost:8080
```

Avec Docker : `docker compose up` (application + simulateur). L'image JMeter avec le plugin et ses dépendances
se construit avec `docker compose --profile tools build jmeter`.

Déploiement gratuit (Hugging Face, Render, VM Oracle) et guide de test : voir `DEPLOIEMENT.md`.

Pour activer l'IA : `export ANTHROPIC_API_KEY=...` (ou un modèle interne compatible OpenAI, voir `config/llm.yaml`).
Sans clé, tout fonctionne en mode règles.

---

## Ce que fait chaque module

| Module | Dossier | IA | Rôle |
|---|---|---|---|
| Noyau ISO 8583 | `isotestforge/iso` | non | Codec piloté par le **même packager jPOS XML** que JMeter, BER-TLV (DE55), canaux ASCII/NAC/NCC |
| Crypto | `isotestforge/crypto` | non | KCV, PIN block ISO 0/1/3, MAC ISO 9797 alg. 1/3, dérivation option A, clé de session, ARQC/ARPC (CVN10/18), CVV/iCVV/CVV2, PVV |
| Référentiel | `data/referential`, `isotestforge/referential` | non | Cartes, terminaux, jeux de clés, specs hôte, templates, BIN de test autorisés |
| DSL | `isotestforge/dsl` | non | Contrat entre IA et moteur, validé par JSON Schema (`itf schema`) |
| Analyste | `isotestforge/agents` | règles puis IA légère | Besoin -> intentions de test + questions au testeur |
| Concepteur | `isotestforge/agents` | bibliothèque, règles, puis IA moyenne | Intentions -> scénarios DSL, vérifiés par un dry-run du moteur |
| Combinatoire | `engine/pairwise.py` | non | Pairwise : 18 combinaisons -> 9 dans l'exemple |
| Mutateurs | `engine/mutators.py` | non | 14 cas négatifs (PIN faux, expirée, MAC, ARQC, CVV, doublon, timeout + 0420...) |
| Data Binder | `engine/binder.py` | non | Règles métier (mode d'entrée, CVM, plafond sans contact, DE90, chaînages), EMV, crypto |
| Builder JMX | `engine/jmx_builder.py` | non | Plan fonctionnel + plan de charge, `keys.properties` séparé, `manifest.json` de traçabilité |
| Validateur | `engine/validator.py` | non | Formats vs packager, champs obligatoires par MTI, PAN de test, structure JMX |
| Simulateur | `simulator/host.py` | non | Émetteur/switch de test, montants magiques, règles YAML, traces masquées |
| Runners | `runner/` | non | JMeter (local ou Docker) et runner natif Python (CI sans JMeter) |
| Analyse | `analysis/` | base puis IA sur échec | JTL -> statuts par scénario et par exigence, diagnostics, HTML et Excel |
| Couverture | `analysis/coverage.py` | non | Matrice carte × opération, annulations, codes réponse jamais provoqués, suggestions DSL |
| Outils | `tools/` | non | Décodeur, import de traces, diff de specs, import de besoins |
| Outilleur | `agents/agents.py` | IA | Propose un nouveau template pour les besoins récurrents non couverts (après revue humaine, plus d'IA) |
| Orchestrateur | `orchestrator/` | — | Workflow avec 2 validations humaines, reprise, bibliothèque, apprentissage des corrections |
| API + UI | `api/` | — | Starlette + page web unique |

## Le workflow du testeur

1. **Besoins** : coller du texte (`REQ-12 : ...` conserve l'identifiant) ou importer un fichier / une requête Jira.
2. **Intentions** : vérifier et corriger le découpage ; répondre aux questions signalées (canal non précisé...).
3. **DSL** : relire la suite, voir l'aperçu des messages champ par champ ; toute correction est mémorisée et sert
   d'exemple aux générations suivantes.
4. **JMX** : télécharger le plan, le packager, les données de charge et `keys.properties`.
5. **Résultats** : exécuter, lire le rapport (diagnostics par code réponse) et la couverture.

En CI, `--execute` et le mode automatique passent les validations humaines.

## Le DSL en bref

```yaml
suite: ACQUIRING_POS_V1
host: SWITCH_SIM
defaults: {card: VISA_EMV_PIN, terminal: POS_ATTENDED, amount: 150.00}
scenarios:
  - id: POS-EMV-001
    title: Achat EMV PIN online puis annulation
    requirement: [REQ-042]
    steps:
      - {id: auth, template: PURCHASE, expect: {"38": present}}
      - {id: rev, template: REVERSAL, ref: auth}        # DE90, STAN, DE7 repris automatiquement
  - id: POS-NEG-PIN
    title: PIN erroné
    derived_from: POS-EMV-001.auth
    mutation: wrong_pin                                  # attendu traduit en DE39 par la spec hôte (55)
  - id: POS-MATRIX
    title: Matrice
    expand: {card: [VISA_EMV_PIN, MC_EMV_PIN], terminal: [POS_ATTENDED, POS_RESTAURANT], amount: [10, 999.99]}
    steps: [{id: auth, template: PURCHASE}]
load: {tps: 20, threads: 10, duration_s: 300}           # génère aussi un plan de charge
```

Attentes : code sémantique (`approved`, `wrong_pin`...), code brut, `present`, `absent`, `re:<regex>`, `timeout`.
Exemples complets : `examples/suites/`.

## Exécuter le JMX dans JMeter

```bash
cd out/acquiring
jmeter -n -t ACQUIRING_POS_V1.jmx -q keys.properties -JITF_HOST=127.0.0.1 -JITF_PORT=8583 -l results.jtl
```

- Plugin : `jmeter-iso8583` 1.4 dans `lib/ext` et ses dépendances (jPOS 2.1.10, BouncyCastle...) dans `lib`
  (voir `docker/jmeter/Dockerfile`), ou `PluginsManagerCMD install tilln-iso8583`.
- Les clés ne sont jamais dans le JMX : `keys.properties` (ou Vault) les fournit au lancement.
- PIN block, MAC et ARQC sont calculés à l'exécution par l'**ISO8583 Crypto PreProcessor**.
- DE39 est contrôlé par le sampler (codes attendus, y compris pour les refus), les autres attentes par une
  assertion Groovy ; les réponses sont mémorisées en variables `R_<scénario>_<étape>_<DE>` pour les chaînages.

## Maîtrise des coûts IA

`config/llm.yaml` : routage par tâche (léger / moyen / lourd), correction ciblée puis escalade, cache exact et
sémantique, cache de prompt sur le préfixe système, API batch pour les générations nocturnes, plafonds par suite
et par jour. `itf usage` et l'onglet « Couverture & coûts » donnent les tokens, le coût et la part traitée sans IA.
Les tarifs sont à renseigner dans `pricing` pour un coût exact.

## Sécurité

- PAN refusés hors des plages de `bin_whitelist.yaml` ; traces importées remappées sur des cartes de test.
- Filtre avant LLM (PAN, piste, clés, PIN blocks masqués) ; le LLM ne voit que des identifiants de profils.
- Clés de test uniquement, `env:` / `vault:` supportés ; `keys.properties` exclu de git.
- Fournisseur LLM interne possible (`provider: openai_compatible`) pour la souveraineté des données.

## Commandes

`itf generate | validate | run | pipeline | serve | simulator | decode | traces | spec-diff | cards | keys |
negatives | coverage | usage | schema` — `itf <commande> -h` pour le détail.

## Points à valider sur votre plateforme

- **JMeter réel** : la structure du JMX suit exactement l'exemple officiel du plugin et passe le validateur,
  mais ce dépôt a été testé avec le runner natif contre le simulateur, pas avec un binaire JMeter.
  Premier lancement recommandé : `itf simulator` puis le plan d'exemple dans JMeter.
- **ARQC** : le simulateur suit EMV/VIS (CVN10, CVN18). Si votre version de jPOS calcule différemment,
  passer `verify_arqc: false` dans `config/simulator.yaml` ou ajuster `crypto/emv.py`.
- **MAC** : algorithme et périmètre alignés sur le plugin (ISO 9797 alg. 3, padding ISO 7816-4, message packé
  sans le champ MAC) ; à confirmer avec la spec de l'hôte cible.
- **Specs hôte** : compléter `hosts.yaml` (champs obligatoires, codes réponse) et le packager de votre switch.

## Tests

`pytest -q` : vecteurs crypto de référence, roundtrip packagers ASCII et BCD, règles métier, structure JMX,
exécution complète des deux suites d'exemple contre le simulateur, pipeline besoins -> rapport, boucle de
correction LLM simulée, API.
