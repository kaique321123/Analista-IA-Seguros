"""Gerador determinístico de dados sintéticos realistas de seguros em DuckDB.

Todos os dados são 100% sintéticos e reproduzíveis (seed fixa), modelados para
simular uma carteira analítica de seguradora brasileira entre 2023 e 2025.
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

SEED = 42
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "seguros.duckdb"

REGIOES_UF: dict[str, list[str]] = {
    "Sudeste": ["SP", "RJ", "MG"],
    "Sul": ["PR", "RS", "SC"],
    "Nordeste": ["BA", "PE", "CE"],
    "Centro-Oeste": ["DF", "GO"],
    "Norte": ["AM", "PA"],
}
REGIAO_PESOS = [0.46, 0.20, 0.18, 0.10, 0.06]

RAMOS = ["Auto", "Residencial", "Vida", "Empresarial"]
RAMO_CONFIG = {
    "Auto": {
        "peso": 0.42,
        "premio_medio": 2850.0,
        "premio_std": 750.0,
        "premio_min": 1100.0,
        "is_mult": 28.0,
        "freq_sinistro": 0.24,
        "sev_ratio": (0.35, 1.85),
        "tipos_sinistro": ["Colisao", "Roubo/Furto", "Assistencia", "Vidros"],
        "tipos_pesos": [0.48, 0.24, 0.18, 0.10],
        "taxa_renovacao_base": 0.72,
    },
    "Residencial": {
        "peso": 0.26,
        "premio_medio": 960.0,
        "premio_std": 280.0,
        "premio_min": 350.0,
        "is_mult": 260.0,
        "freq_sinistro": 0.14,
        "sev_ratio": (0.45, 2.20),
        "tipos_sinistro": ["Danos Eletricos", "Assistencia", "Incendio", "Roubo/Furto"],
        "tipos_pesos": [0.40, 0.35, 0.10, 0.15],
        "taxa_renovacao_base": 0.77,
    },
    "Vida": {
        "peso": 0.20,
        "premio_medio": 1450.0,
        "premio_std": 450.0,
        "premio_min": 420.0,
        "is_mult": 180.0,
        "freq_sinistro": 0.05,
        "sev_ratio": (1.80, 5.50),
        "tipos_sinistro": ["Falecimento/Invalidez", "Doenca Grave", "Assistencia"],
        "tipos_pesos": [0.35, 0.30, 0.35],
        "taxa_renovacao_base": 0.81,
    },
    "Empresarial": {
        "peso": 0.12,
        "premio_medio": 6800.0,
        "premio_std": 2400.0,
        "premio_min": 2200.0,
        "is_mult": 150.0,
        "freq_sinistro": 0.18,
        "sev_ratio": (0.50, 2.80),
        "tipos_sinistro": [
            "Responsabilidade Civil",
            "Danos Eletricos",
            "Incendio",
            "Roubo/Furto",
        ],
        "tipos_pesos": [0.32, 0.28, 0.18, 0.22],
        "taxa_renovacao_base": 0.75,
    },
}

CANAIS = ["Corretor", "Digital", "Bancassurance", "Parcerias"]
CANAL_PESOS = [0.44, 0.25, 0.20, 0.11]
CANAL_RENOVACAO_BONUS = {
    "Corretor": 0.03,
    "Digital": -0.07,
    "Bancassurance": 0.06,
    "Parcerias": -0.04,
}

MOTIVOS_NAO_RENOVACAO = [
    "Preco",
    "Concorrencia",
    "Atendimento",
    "Sinistro",
    "Desistencia",
]
MOTIVOS_PESOS = [0.42, 0.24, 0.14, 0.10, 0.10]


def _random_date(rng: random.Random, start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=rng.randint(0, max(delta, 0)))


def build_datasets(
    n_clientes: int = 2000,
    n_apolices: int = 4500,
    seed: int = SEED,
) -> dict[str, pd.DataFrame]:
    """Gera DataFrames sintéticos de clientes, apólices, renovações e sinistros."""
    rng = random.Random(seed)
    ref_hoje = date(2025, 12, 31)

    # 1. Clientes
    clientes_rows = []
    regioes_lista = list(REGIOES_UF.keys())
    for cid in range(1, n_clientes + 1):
        tipo_pessoa = "PJ" if rng.random() < 0.15 else "PF"
        if tipo_pessoa == "PJ":
            faixa_etaria = "Empresa"
            segmento = rng.choices(["PME", "Corporate"], weights=[0.72, 0.28])[0]
        else:
            faixa_etaria = rng.choices(
                ["18-29", "30-44", "45-59", "60+"], weights=[0.18, 0.38, 0.29, 0.15]
            )[0]
            segmento = rng.choices(["Varejo", "Afluente"], weights=[0.74, 0.26])[0]

        regiao = rng.choices(regioes_lista, weights=REGIAO_PESOS)[0]
        uf = rng.choice(REGIOES_UF[regiao])
        data_cadastro = _random_date(rng, date(2022, 1, 1), date(2025, 10, 31))

        clientes_rows.append(
            {
                "cliente_id": cid,
                "tipo_pessoa": tipo_pessoa,
                "faixa_etaria": faixa_etaria,
                "regiao": regiao,
                "uf": uf,
                "segmento": segmento,
                "data_cadastro": data_cadastro,
            }
        )
    df_clientes = pd.DataFrame(clientes_rows)
    cliente_lookup = df_clientes.set_index("cliente_id").to_dict("index")

    # 2. Apólices
    apolices_rows = []
    renovacoes_rows = []
    sinistros_rows = []

    renovacao_id = 1
    sinistro_id = 1

    # Sazonalidade mensal (Q4 e março/abril mais fortes em vendas)
    mes_pesos = [0.90, 0.85, 1.05, 1.05, 0.98, 0.95, 0.96, 1.00, 1.02, 1.08, 1.12, 1.15]

    for aid in range(1, n_apolices + 1):
        cid = rng.randint(1, n_clientes)
        cinfo = cliente_lookup[cid]

        # Ramo coerente com tipo de pessoa
        if cinfo["tipo_pessoa"] == "PJ":
            ramo = rng.choices(["Empresarial", "Auto", "Vida"], weights=[0.65, 0.25, 0.10])[0]
        else:
            ramo = rng.choices(
                ["Auto", "Residencial", "Vida"], weights=[0.48, 0.30, 0.22]
            )[0]

        cfg = RAMO_CONFIG[ramo]
        canal = rng.choices(CANAIS, weights=CANAL_PESOS)[0]

        # Data de emissão entre 2023-01-01 e 2025-12-15 respeitando data_cadastro
        ano = rng.choices([2023, 2024, 2025], weights=[0.30, 0.34, 0.36])[0]
        mes = rng.choices(range(1, 13), weights=mes_pesos)[0]
        dia = rng.randint(1, 28)
        data_emissao = date(ano, mes, dia)
        if data_emissao < cinfo["data_cadastro"]:
            data_emissao = cinfo["data_cadastro"] + timedelta(days=rng.randint(0, 20))
        if data_emissao > date(2025, 12, 15):
            data_emissao = date(2025, 12, 15)

        data_inicio = data_emissao + timedelta(days=rng.randint(0, 7))
        data_fim = data_inicio + timedelta(days=365)

        # Prêmio com leve variação por segmento e inflação anual (~6%/ano)
        fator_seg = {"Varejo": 0.92, "Afluente": 1.28, "PME": 1.00, "Corporate": 1.65}[
            cinfo["segmento"]
        ]
        fator_ano = {2023: 0.94, 2024: 1.00, 2025: 1.07}[data_emissao.year]
        premio = max(
            cfg["premio_min"],
            rng.gauss(cfg["premio_medio"] * fator_seg * fator_ano, cfg["premio_std"]),
        )
        premio = round(premio, 2)
        importancia_segurada = round(premio * cfg["is_mult"] * rng.uniform(0.85, 1.20), 2)

        # Status da apólice na data de referência (2025-12-31)
        if data_fim <= ref_hoje:
            status = "Cancelada" if rng.random() < 0.08 else "Vencida"
        else:
            status = "Cancelada" if rng.random() < 0.06 else "Ativa"

        apolices_rows.append(
            {
                "apolice_id": aid,
                "cliente_id": cid,
                "ramo": ramo,
                "canal_venda": canal,
                "data_emissao": data_emissao,
                "data_inicio_vigencia": data_inicio,
                "data_fim_vigencia": data_fim,
                "premio_emitido": premio,
                "importancia_segurada": importancia_segurada,
                "status": status,
            }
        )

        # 3. Sinistros durante a vigência (até ref_hoje)
        fim_observacao = min(data_fim, ref_hoje)
        dias_expostos = max((fim_observacao - data_inicio).days, 0)
        teve_sinistro = False
        if dias_expostos > 15:
            prob_sinistro = cfg["freq_sinistro"] * (dias_expostos / 365.0)
            # Regiões metropolitanas do Sudeste têm leve alta em Auto
            if ramo == "Auto" and cinfo["uf"] in ("SP", "RJ"):
                prob_sinistro *= 1.15
            if rng.random() < min(prob_sinistro, 0.85):
                teve_sinistro = True
                n_sinistros = 2 if rng.random() < 0.12 else 1
                for _ in range(n_sinistros):
                    data_ocorrencia = _random_date(rng, data_inicio, fim_observacao)
                    atraso_aviso = rng.randint(0, 12)
                    data_aviso = min(data_ocorrencia + timedelta(days=atraso_aviso), ref_hoje)
                    tipo_sinistro = rng.choices(
                        cfg["tipos_sinistro"], weights=cfg["tipos_pesos"]
                    )[0]

                    # Sinistros recentes têm maior chance de ainda estarem Em Analise
                    dias_desde_aviso = (ref_hoje - data_aviso).days
                    if dias_desde_aviso < 30:
                        status_sinistro = rng.choices(
                            ["Pago", "Em Analise", "Negado"], weights=[0.35, 0.55, 0.10]
                        )[0]
                    else:
                        status_sinistro = rng.choices(
                            ["Pago", "Em Analise", "Negado"], weights=[0.80, 0.07, 0.13]
                        )[0]

                    lo, hi = cfg["sev_ratio"]
                    valor_reclamado = round(premio * rng.uniform(lo, hi), 2)
                    if status_sinistro == "Pago":
                        # Franquia / regulação reduz levemente o valor pago frente ao reclamado
                        valor_pago = round(valor_reclamado * rng.uniform(0.75, 1.00), 2)
                    else:
                        valor_pago = 0.0

                    sinistros_rows.append(
                        {
                            "sinistro_id": sinistro_id,
                            "apolice_id": aid,
                            "cliente_id": cid,
                            "ramo": ramo,
                            "data_ocorrencia": data_ocorrencia,
                            "data_aviso": data_aviso,
                            "tipo_sinistro": tipo_sinistro,
                            "status_sinistro": status_sinistro,
                            "valor_reclamado": valor_reclamado,
                            "valor_pago": valor_pago,
                        }
                    )
                    sinistro_id += 1

        # 4. Renovações (apenas para apólices cuja vigência venceu até ref_hoje)
        if data_fim <= ref_hoje:
            elegivel = status != "Cancelada"
            if elegivel:
                prob_renovar = cfg["taxa_renovacao_base"] + CANAL_RENOVACAO_BONUS[canal]
                if teve_sinistro:
                    prob_renovar -= 0.08
                if cinfo["segmento"] in ("Afluente", "Corporate"):
                    prob_renovar += 0.04
                prob_renovar = max(0.25, min(0.95, prob_renovar))
                renovada = rng.random() < prob_renovar
            else:
                renovada = False

            if renovada:
                reajuste = rng.uniform(1.03, 1.18) if teve_sinistro else rng.uniform(0.98, 1.10)
                premio_renovado = round(premio * reajuste, 2)
                motivo = None
            else:
                premio_renovado = None
                motivo = rng.choices(MOTIVOS_NAO_RENOVACAO, weights=MOTIVOS_PESOS)[0]

            renovacoes_rows.append(
                {
                    "renovacao_id": renovacao_id,
                    "apolice_id": aid,
                    "cliente_id": cid,
                    "ramo": ramo,
                    "data_vencimento": data_fim,
                    "elegivel_renovacao": elegivel,
                    "renovada": renovada,
                    "motivo_nao_renovacao": motivo,
                    "premio_anterior": premio,
                    "premio_renovado": premio_renovado,
                }
            )
            renovacao_id += 1

    return {
        "clientes": df_clientes,
        "apolices": pd.DataFrame(apolices_rows),
        "renovacoes": pd.DataFrame(renovacoes_rows),
        "sinistros": pd.DataFrame(sinistros_rows),
    }


def generate_duckdb(
    db_path: Path = DEFAULT_DB_PATH,
    n_clientes: int = 2000,
    n_apolices: int = 4500,
    seed: int = SEED,
) -> dict[str, int]:
    """Cria (ou recria) o banco DuckDB com as tabelas sintéticas de seguros."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    datasets = build_datasets(n_clientes=n_clientes, n_apolices=n_apolices, seed=seed)

    con = duckdb.connect(str(db_path))
    try:
        for table_name, df in datasets.items():
            con.register(f"tmp_{table_name}", df)
            con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM tmp_{table_name}")
            con.unregister(f"tmp_{table_name}")

        counts = {
            name: con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in datasets
        }
    finally:
        con.close()

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gera banco analítico sintético de seguros em DuckDB."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Caminho do arquivo .duckdb de saída.",
    )
    parser.add_argument("--clientes", type=int, default=2000)
    parser.add_argument("--apolices", type=int, default=4500)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    counts = generate_duckdb(
        db_path=args.output,
        n_clientes=args.clientes,
        n_apolices=args.apolices,
        seed=args.seed,
    )
    print(f"Banco DuckDB gerado com sucesso em: {args.output}")
    for table, count in counts.items():
        print(f"  - {table}: {count:,} registros")


if __name__ == "__main__":
    main()
