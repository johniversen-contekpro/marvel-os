#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marvel OS — démo. Pose 5-6 questions réelles à l'agent, en langage naturel.
But : montrer que la base n'est pas une archive mais un moteur de décisions
(croise marque / thèmes / entreprises / contenu, réponses sourcées).

Prérequis : ANTHROPIC_API_KEY + base ingérée (python ingest_chorus.py).
"""
import os, sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import anthropic
import marvel_agent as agent

QUESTIONS = [
    "Répartition des calls par marque, et pour Mobile Culinaire les 5 entreprises les plus récurrentes.",
    "Quelles objections ou frictions reviennent sur le permitting ? Cite 2-3 calls Modular précis.",
    "Quels calls mentionnent des data centers ou des camps de travailleurs reculés (workforce camps) ? "
    "C'est un vertical émergent pour Mobile ?",
    "Quelles marques d'équipement (fours, réfrigération, hottes) reviennent le plus dans nos fiches ?",
    "Sur les 3 derniers mois de données, quels thèmes dominent côté Modular vs Mobile ?",
]


def main():
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("[!] Définis ANTHROPIC_API_KEY d'abord (la couche données est déjà testable sans).")
        sys.exit(1)
    client = agent.make_client()
    print(f"Marvel OS — démo ({agent.MODEL})\n" + "=" * 70)
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n[{i}] {q}")
        print("-" * 70)
        try:
            print(agent.ask(q, client=client, verbose=True))
        except Exception as e:
            print(f"[ERREUR] {e}")
        print("=" * 70)


if __name__ == "__main__":
    main()
