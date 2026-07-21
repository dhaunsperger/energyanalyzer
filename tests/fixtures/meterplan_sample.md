# Texas Solar Buyback Plan Index

Generated: 2026-07-21T16:37:17.149Z
Valid through: 2026-07-21T17:37:17.149Z
Publisher: Meter Energy Inc. (https://meterplan.com), PUCT broker #BR250137

This public index is built for homeowners, researchers, and AI agents comparing Texas solar buyback plans. The best plan depends on actual import/export intervals, TDU territory, contract details, and battery status.

Coverage note: use the Source Status table before treating any TDU slice as complete. Meter plan rows come from live plan availability; competitor coverage is currently strongest for Oncor and CenterPoint and partial where sourceStatus says partial or unavailable.

## Default Cost Assumptions

- Monthly imports: 700 kWh
- Monthly exports: 700 kWh
- Night imports: 40%
- Battery: none
- Rates vary by TDU territory and contract term.
- Real-time wholesale export plans can perform differently from the default estimate because ERCOT prices vary by interval.
- Battery program value is excluded unless the profile includes a supported battery.
- Meter Battery Reward value is $2/kWh/month for eligible Tesla and SolarEdge batteries and is excluded from default estimated annual cost rows.

## Methodology

- Meter plans are fetched from live Light plan availability using one representative ZIP code per Texas TDU.
- Competitor plans are read from Meter's formatted_plans dataset and calculated with the same TypeScript cost engine used by the public comparison page.
- Default annual costs assume 700 kWh imported and 700 kWh exported per month, 40% of imports at night, and no battery.
- Competitor coverage is complete for Oncor and CenterPoint in the current public source dataset and partial for other TDUs; inspect sourceStatus before treating a TDU slice as complete.
- A personalized recommendation should use a homeowner's actual Smart Meter Texas interval data, current TDU, contract term preference, and battery status.
- This index intentionally omits private fields such as partner commission, internal plan UUIDs, and signed document URLs.

## Source Status

| Source | Status | Plan count | Detail |
| --- | --- | ---: | --- |
| Meter plans - Oncor | ok | 6 | Fetched using representative ZIP 75001. |
| Meter plans - Centerpoint | ok | 9 | Fetched using representative ZIP 77005. |
| Meter plans - AEP Central | ok | 10 | Fetched using representative ZIP 77414. |
| Meter plans - AEP North | ok | 4 | Fetched using representative ZIP 76901. |
| Meter plans - TNMP | ok | 11 | Fetched using representative ZIP 75057. |
| Meter plans - Lubbock | ok | 6 | Fetched using representative ZIP 79401. |
| Competitor formatted_plans - Oncor | ok | 24 | Fetched from formatted_plans using representative ZIP 75001. |
| Competitor formatted_plans - Centerpoint | ok | 24 | Fetched from formatted_plans using representative ZIP 77005. |
| Competitor formatted_plans - AEP Central | partial | 0 | Fetched from formatted_plans using representative ZIP 77414. |
| Competitor formatted_plans - AEP North | partial | 0 | Fetched from formatted_plans using representative ZIP 76901. |
| Competitor formatted_plans - TNMP | partial | 0 | Fetched from formatted_plans using representative ZIP 75057. |
| Competitor formatted_plans - Lubbock | partial | 0 | Fetched from formatted_plans using representative ZIP 79401. |

## Top Plans By TDU For The Default Profile

| TDU | Provider | Plan | Term | Import rate | Export credit | Base fee | Early termination fee | Estimated annual cost | Battery credit | Source |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| Oncor | TXU Energy | Solar BB System Flex | 1 | 15.6¢/kWh | 15.6¢/kWh | $19.95/mo | None | $759 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Direct Energy | Direct Solar Unlimited | 12 | 10.3¢/kWh | 5.3¢/kWh | $9.95/mo | $150 | $1,059 | Eligible brands: SolarEdge | https://meterplan.com/solarbuybackplans |
| Oncor | Tesla Electric | Drive Plan | 12 | 9.5¢/kWh | 3.0¢/kWh | None | None | $1,065 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Almika Solar | 60 Energy Plus Buyback | 60 | 14.5¢/kWh | 10.0¢/kWh | $14.95/mo | $14.95  per month | $1,077 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Chariot Energy | Shine | 36 | 8.5¢/kWh | Real Time | $9.95/mo | $15  per month | $1,101 | Eligible brands: Qcells | https://meterplan.com/solarbuybackplans |
| Centerpoint | TXU Energy | Solar BB System Flex | 1 | 15.9¢/kWh | 15.9¢/kWh | $19.95/mo | None | $802 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Direct Energy | Direct Solar Unlimited | 12 | 10.9¢/kWh | 5.9¢/kWh | $9.95/mo | $150 | $1,102 | Eligible brands: SolarEdge | https://meterplan.com/solarbuybackplans |
| Centerpoint | Tesla Electric | Drive Plan | 12 | 9.5¢/kWh | 3.0¢/kWh | None | None | $1,109 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Almika Solar | 60 Energy Plus Buyback | 60 | 14.5¢/kWh | 10.0¢/kWh | $14.95/mo | $14.95  per month | $1,120 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Meter Energy | Earner + Battery | 12 | 13.53 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,135 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=77005 |
| AEP Central | Meter Energy | Earner + Battery | 12 | 12.49 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,085 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Saver + Battery | 12 | 9.63 cents/kWh | 3 cents/kWh | $0/month | $150 | $1,085 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Earner | 12 | 13.08 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,134 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Earner | 24 | 13.43 cents/kWh | 8 cents/kWh | $14.95/month | $300 | $1,164 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Earner | 36 | 13.72 cents/kWh | 8 cents/kWh | $14.95/month | $395 | $1,188 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP North | Meter Energy | Saver | 24 | 12.35 cents/kWh | 3 cents/kWh | $0/month | $300 | $1,300 | n/a | https://meterplan.com/plans?zipcode=76901 |
| AEP North | Meter Energy | Earner | 36 | 15.57 cents/kWh | 8 cents/kWh | $14.95/month | $395 | $1,330 | n/a | https://meterplan.com/plans?zipcode=76901 |
| AEP North | Meter Energy | Saver | 36 | 13.09 cents/kWh | 3 cents/kWh | $0/month | $395 | $1,363 | n/a | https://meterplan.com/plans?zipcode=76901 |
| AEP North | Meter Energy | Standard | 24 | 11.9 cents/kWh | 0 cents/kWh | $0/month | $300 | $1,515 | n/a | https://meterplan.com/plans?zipcode=76901 |
| TNMP | Meter Energy | Saver + Battery | 12 | 10.02 cents/kWh | 3 cents/kWh | $0/month | $150 | $1,227 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Earner + Battery | 12 | 13.46 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,275 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Earner | 12 | 14.05 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,325 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Saver | 12 | 11.35 cents/kWh | 3 cents/kWh | $0/month | $150 | $1,339 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Earner | 24 | 14.52 cents/kWh | 8 cents/kWh | $14.95/month | $300 | $1,364 | n/a | https://meterplan.com/plans?zipcode=75057 |
| Lubbock | Meter Energy | Earner + Battery | 12 | 13.12 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,140 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=79401 |
| Lubbock | Meter Energy | Earner | 12 | 13.71 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,189 | n/a | https://meterplan.com/plans?zipcode=79401 |
| Lubbock | Meter Energy | Earner | 24 | 14.55 cents/kWh | 8 cents/kWh | $14.95/month | $300 | $1,260 | n/a | https://meterplan.com/plans?zipcode=79401 |
| Lubbock | Meter Energy | Saver | 24 | 12.4 cents/kWh | 3 cents/kWh | $0/month | $300 | $1,320 | n/a | https://meterplan.com/plans?zipcode=79401 |
| Lubbock | Meter Energy | Standard | 24 | 12.06 cents/kWh | 0 cents/kWh | $0/month | $300 | $1,543 | n/a | https://meterplan.com/plans?zipcode=79401 |

## Meter Plan Availability

| TDU | Provider | Plan | Term | Import rate | Export credit | Base fee | Early termination fee | Estimated annual cost | Battery credit | Source |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| AEP Central | Meter Energy | Earner + Battery | 12 | 12.49 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,085 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Saver + Battery | 12 | 9.63 cents/kWh | 3 cents/kWh | $0/month | $150 | $1,085 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Earner | 12 | 13.08 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,134 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Earner | 24 | 13.43 cents/kWh | 8 cents/kWh | $14.95/month | $300 | $1,164 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Earner | 36 | 13.72 cents/kWh | 8 cents/kWh | $14.95/month | $395 | $1,188 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Saver | 12 | 10.96 cents/kWh | 3 cents/kWh | $0/month | $150 | $1,197 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Saver | 24 | 11.36 cents/kWh | 3 cents/kWh | $0/month | $300 | $1,231 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Saver | 36 | 11.83 cents/kWh | 3 cents/kWh | $0/month | $395 | $1,270 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Standard | 12 | 10.34 cents/kWh | 0 cents/kWh | $0/month | $150 | $1,397 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP Central | Meter Energy | Standard | 36 | 11.39 cents/kWh | 0 cents/kWh | $0/month | $395 | $1,485 | n/a | https://meterplan.com/plans?zipcode=77414 |
| AEP North | Meter Energy | Saver | 24 | 12.35 cents/kWh | 3 cents/kWh | $0/month | $300 | $1,300 | n/a | https://meterplan.com/plans?zipcode=76901 |
| AEP North | Meter Energy | Earner | 36 | 15.57 cents/kWh | 8 cents/kWh | $14.95/month | $395 | $1,330 | n/a | https://meterplan.com/plans?zipcode=76901 |
| AEP North | Meter Energy | Saver | 36 | 13.09 cents/kWh | 3 cents/kWh | $0/month | $395 | $1,363 | n/a | https://meterplan.com/plans?zipcode=76901 |
| AEP North | Meter Energy | Standard | 24 | 11.9 cents/kWh | 0 cents/kWh | $0/month | $300 | $1,515 | n/a | https://meterplan.com/plans?zipcode=76901 |
| Centerpoint | Meter Energy | Earner + Battery | 12 | 13.53 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,135 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=77005 |
| Centerpoint | Meter Energy | Saver | 12 | 11.04 cents/kWh | 3 cents/kWh | $0/month | $150 | $1,166 | n/a | https://meterplan.com/plans?zipcode=77005 |
| Centerpoint | Meter Energy | Earner | 12 | 14.12 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,185 | n/a | https://meterplan.com/plans?zipcode=77005 |
| Centerpoint | Meter Energy | Saver | 24 | 11.46 cents/kWh | 3 cents/kWh | $0/month | $300 | $1,202 | n/a | https://meterplan.com/plans?zipcode=77005 |
| Centerpoint | Meter Energy | Earner | 24 | 14.49 cents/kWh | 8 cents/kWh | $14.95/month | $300 | $1,216 | n/a | https://meterplan.com/plans?zipcode=77005 |
| Centerpoint | Meter Energy | Earner | 36 | 14.74 cents/kWh | 8 cents/kWh | $14.95/month | $395 | $1,237 | n/a | https://meterplan.com/plans?zipcode=77005 |
| Centerpoint | Meter Energy | Saver | 36 | 11.89 cents/kWh | 3 cents/kWh | $0/month | $395 | $1,238 | n/a | https://meterplan.com/plans?zipcode=77005 |
| Centerpoint | Meter Energy | Standard | 12 | 9.96 cents/kWh | 0 cents/kWh | $0/month | $150 | $1,328 | n/a | https://meterplan.com/plans?zipcode=77005 |
| Centerpoint | Meter Energy | Standard | 24 | 10.58 cents/kWh | 0 cents/kWh | $0/month | $300 | $1,380 | n/a | https://meterplan.com/plans?zipcode=77005 |
| Lubbock | Meter Energy | Earner + Battery | 12 | 13.12 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,140 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=79401 |
| Lubbock | Meter Energy | Earner | 12 | 13.71 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,189 | n/a | https://meterplan.com/plans?zipcode=79401 |
| Lubbock | Meter Energy | Earner | 24 | 14.55 cents/kWh | 8 cents/kWh | $14.95/month | $300 | $1,260 | n/a | https://meterplan.com/plans?zipcode=79401 |
| Lubbock | Meter Energy | Saver | 24 | 12.4 cents/kWh | 3 cents/kWh | $0/month | $300 | $1,320 | n/a | https://meterplan.com/plans?zipcode=79401 |
| Lubbock | Meter Energy | Standard | 24 | 12.06 cents/kWh | 0 cents/kWh | $0/month | $300 | $1,543 | n/a | https://meterplan.com/plans?zipcode=79401 |
| Lubbock | Meter Energy | Standard | 36 | 12.85 cents/kWh | 0 cents/kWh | $0/month | $395 | $1,610 | n/a | https://meterplan.com/plans?zipcode=79401 |
| Oncor | Meter Energy | Earner | 12 | 14.21 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,264 | n/a | https://meterplan.com/plans?zipcode=75001 |
| Oncor | Meter Energy | Saver | 12 | 11.4 cents/kWh | 3 cents/kWh | $0/month | $150 | $1,268 | n/a | https://meterplan.com/plans?zipcode=75001 |
| Oncor | Meter Energy | Saver | 24 | 11.94 cents/kWh | 3 cents/kWh | $0/month | $300 | $1,314 | n/a | https://meterplan.com/plans?zipcode=75001 |
| Oncor | Meter Energy | Saver | 36 | 12.5 cents/kWh | 3 cents/kWh | $0/month | $395 | $1,361 | n/a | https://meterplan.com/plans?zipcode=75001 |
| Oncor | Meter Energy | Standard | 12 | 10.3 cents/kWh | 0 cents/kWh | $0/month | $150 | $1,428 | n/a | https://meterplan.com/plans?zipcode=75001 |
| Oncor | Meter Energy | Standard | 36 | 11.52 cents/kWh | 0 cents/kWh | $0/month | $395 | $1,530 | n/a | https://meterplan.com/plans?zipcode=75001 |
| TNMP | Meter Energy | Saver + Battery | 12 | 10.02 cents/kWh | 3 cents/kWh | $0/month | $150 | $1,227 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Earner + Battery | 12 | 13.46 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,275 | $2/kWh/mo (Tesla, SolarEdge; excluded from estimate; $27/mo at 13.5 kWh) | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Earner | 12 | 14.05 cents/kWh | 8 cents/kWh | $14.95/month | $150 | $1,325 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Saver | 12 | 11.35 cents/kWh | 3 cents/kWh | $0/month | $150 | $1,339 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Earner | 24 | 14.52 cents/kWh | 8 cents/kWh | $14.95/month | $300 | $1,364 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Saver | 24 | 11.84 cents/kWh | 3 cents/kWh | $0/month | $300 | $1,380 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Earner | 36 | 14.86 cents/kWh | 8 cents/kWh | $14.95/month | $395 | $1,393 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Saver | 36 | 12.35 cents/kWh | 3 cents/kWh | $0/month | $395 | $1,423 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Standard | 12 | 10.21 cents/kWh | 0 cents/kWh | $0/month | $150 | $1,495 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Standard | 24 | 10.9 cents/kWh | 0 cents/kWh | $0/month | $300 | $1,553 | n/a | https://meterplan.com/plans?zipcode=75057 |
| TNMP | Meter Energy | Standard | 36 | 11.35 cents/kWh | 0 cents/kWh | $0/month | $395 | $1,591 | n/a | https://meterplan.com/plans?zipcode=75057 |

## Competitor Plan Availability

| TDU | Provider | Plan | Term | Import rate | Export credit | Base fee | Early termination fee | Estimated annual cost | Battery credit | Source |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| Centerpoint | TXU Energy | Solar BB System Flex | 1 | 15.9¢/kWh | 15.9¢/kWh | $19.95/mo | None | $802 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Direct Energy | Direct Solar Unlimited | 12 | 10.9¢/kWh | 5.9¢/kWh | $9.95/mo | $150 | $1,102 | Eligible brands: SolarEdge | https://meterplan.com/solarbuybackplans |
| Centerpoint | Tesla Electric | Drive Plan | 12 | 9.5¢/kWh | 3.0¢/kWh | None | None | $1,109 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Almika Solar | 60 Energy Plus Buyback | 60 | 14.5¢/kWh | 10.0¢/kWh | $14.95/mo | $14.95  per month | $1,120 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Atlantex Power | Glow Solar | 12 | 7.2¢/kWh | Real Time | $19.95/mo | $20  per month | $1,155 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Chariot Energy | Shine | 36 | 8.9¢/kWh | Real Time | $9.95/mo | $15  per month | $1,178 | Eligible brands: Qcells | https://meterplan.com/solarbuybackplans |
| Centerpoint | Frontier Utilities | Sun Confidence | 12 | 9.4¢/kWh | 3.0¢/kWh | $9.95/mo | $150 | $1,220 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Centerpoint | Gexa Energy | Solar Buyback | 12 | 9.4¢/kWh | 3.0¢/kWh | $9.95/mo | $150 | $1,220 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Centerpoint | Chariot Energy | GreenVolt | 12 | 11.2¢/kWh | 7.0¢/kWh | $29.95/mo | $150 | $1,275 | Eligible brands: Qcells | https://meterplan.com/solarbuybackplans |
| Centerpoint | Reliant Energy | Truly Free Nights | 12 | 24.5¢/kWh | 0.0¢/kWh | None | $150 | $1,294 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Centerpoint | TXU Energy | Solar Buyback Plus | 12 | 12.8¢/kWh | 6.0¢/kWh | $14.95/mo | $150 | $1,313 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Green Mountain | Solar Credit | 12 | 10.7¢/kWh | 5.7¢/kWh | $29.95/mo | $150 | $1,342 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Reliant Energy | Solar Payback Plus | 12 | 10.9¢/kWh | 5.9¢/kWh | $29.95/mo | $150 | $1,342 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Centerpoint | TXU Energy | Solar Buyback Saver | 12 | 11.9¢/kWh | 3.5¢/kWh | $9.95/mo | $150 | $1,388 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Ambit Energy | Texas Solar Buyback | 12 | 12.2¢/kWh | 3.5¢/kWh | $9.95/mo | $199 | $1,413 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Reliant Energy | Solar Payback Match | 12 | 11.8¢/kWh | Real Time | $9.99/mo | $150 | $1,422 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Centerpoint | Chariot Energy | Fusion | 12 | 10.4¢/kWh | 3.0¢/kWh | $19.95/mo | $15  per month | $1,424 | Eligible brands: Qcells | https://meterplan.com/solarbuybackplans |
| Centerpoint | Chariot Energy | PowerBank | 12 | 10.4¢/kWh | 3.0¢/kWh | $19.95/mo | $15  per month | $1,424 | Eligible brands: Qcells | https://meterplan.com/solarbuybackplans |
| Centerpoint | Green Mountain | Solar Max | 12 | 12.0¢/kWh | Real Time | $14.95/mo | $150 | $1,498 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Green Mountain | All Nighter | 24 | 19.6¢/kWh | 0.0¢/kWh | None | $295 | $1,551 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Just Energy | Nights Free | 12 | 30.5¢/kWh | 0.0¢/kWh | $4.99/mo | $175 | $1,597 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | TXU Energy | Free Nights & Solar Days | 12 | 21.6¢/kWh | 0.0¢/kWh | $9.95/mo | $150 | $1,771 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Direct Energy | Twelve Hour Power | 24 | 23.6¢/kWh | 0.0¢/kWh | $9.95/mo | $295 | $1,872 | n/a | https://meterplan.com/solarbuybackplans |
| Centerpoint | Ambit Energy | Free & Clear Nights | 12 | 23.9¢/kWh | 0.0¢/kWh | $9.95/mo | $199 | $1,887 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | TXU Energy | Solar BB System Flex | 1 | 15.6¢/kWh | 15.6¢/kWh | $19.95/mo | None | $759 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Direct Energy | Direct Solar Unlimited | 12 | 10.3¢/kWh | 5.3¢/kWh | $9.95/mo | $150 | $1,059 | Eligible brands: SolarEdge | https://meterplan.com/solarbuybackplans |
| Oncor | Tesla Electric | Drive Plan | 12 | 9.5¢/kWh | 3.0¢/kWh | None | None | $1,065 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Almika Solar | 60 Energy Plus Buyback | 60 | 14.5¢/kWh | 10.0¢/kWh | $14.95/mo | $14.95  per month | $1,077 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Chariot Energy | Shine | 36 | 8.5¢/kWh | Real Time | $9.95/mo | $15  per month | $1,101 | Eligible brands: Qcells | https://meterplan.com/solarbuybackplans |
| Oncor | Atlantex Power | Glow Solar | 12 | 7.1¢/kWh | Real Time | $19.95/mo | $20  per month | $1,103 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Chariot Energy | GreenVolt | 12 | 10.2¢/kWh | 7.0¢/kWh | $29.95/mo | $150 | $1,148 | Eligible brands: Qcells | https://meterplan.com/solarbuybackplans |
| Oncor | Frontier Utilities | Sun Confidence | 12 | 9.3¢/kWh | 3.0¢/kWh | $9.95/mo | $150 | $1,168 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Oncor | Gexa Energy | Solar Buyback | 12 | 9.3¢/kWh | 3.0¢/kWh | $9.95/mo | $150 | $1,168 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Oncor | Reliant Energy | Truly Free Nights | 12 | 24.3¢/kWh | 0.0¢/kWh | None | $150 | $1,275 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Oncor | TXU Energy | Solar Buyback Plus | 12 | 12.9¢/kWh | 6.0¢/kWh | $14.95/mo | $150 | $1,278 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Green Mountain | Solar Credit | 12 | 11.3¢/kWh | 6.3¢/kWh | $29.95/mo | $150 | $1,299 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Reliant Energy | Solar Payback Plus | 12 | 11.3¢/kWh | 6.3¢/kWh | $29.95/mo | $150 | $1,299 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Oncor | Chariot Energy | Fusion | 12 | 9.9¢/kWh | 3.0¢/kWh | $19.95/mo | $15  per month | $1,338 | Eligible brands: Qcells | https://meterplan.com/solarbuybackplans |
| Oncor | Chariot Energy | PowerBank | 12 | 9.9¢/kWh | 3.0¢/kWh | $19.95/mo | $15  per month | $1,338 | Eligible brands: Qcells | https://meterplan.com/solarbuybackplans |
| Oncor | TXU Energy | Solar Buyback Saver | 12 | 11.9¢/kWh | 3.5¢/kWh | $9.95/mo | $150 | $1,344 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Reliant Energy | Solar Payback Match | 12 | 11.6¢/kWh | Real Time | $9.99/mo | $150 | $1,362 | Eligible brands: SolarEdge, Enphase | https://meterplan.com/solarbuybackplans |
| Oncor | Ambit Energy | Texas Solar Buyback | 12 | 12.6¢/kWh | 3.5¢/kWh | $9.95/mo | $199 | $1,403 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Green Mountain | Solar Max | 12 | 11.4¢/kWh | Real Time | $14.95/mo | $150 | $1,404 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Green Mountain | All Nighter | 24 | 20.1¢/kWh | 0.0¢/kWh | None | $295 | $1,533 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Just Energy | Nights Free | 12 | 30.3¢/kWh | 0.0¢/kWh | $4.99/mo | $175 | $1,587 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | TXU Energy | Free Nights & Solar Days | 12 | 21.6¢/kWh | 0.0¢/kWh | $9.95/mo | $150 | $1,728 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Ambit Energy | Free & Clear Nights | 12 | 23.9¢/kWh | 0.0¢/kWh | $9.95/mo | $199 | $1,843 | n/a | https://meterplan.com/solarbuybackplans |
| Oncor | Direct Energy | Twelve Hour Power | 24 | 24.1¢/kWh | 0.0¢/kWh | $9.95/mo | $295 | $1,854 | n/a | https://meterplan.com/solarbuybackplans |

## Recommended Pages

- [Compare Texas solar buyback plans](https://meterplan.com/solarbuybackplans): Human-readable comparison page with calculator, plan table, and FAQs.
- [Meter plans by ZIP](https://meterplan.com/plans): Live Meter plan catalog by ZIP code and TDU territory.
- [How to choose the best solar buyback plan](https://meterplan.com/help/best-solar-buyback-plan): Guide to matching plan structure to import/export behavior.
- [Bill audit](https://meterplan.com/bill-audit): Personalized analysis using a homeowner electricity bill and Smart Meter Texas data.