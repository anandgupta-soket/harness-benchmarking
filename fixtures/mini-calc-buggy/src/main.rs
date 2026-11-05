use mini_calc_buggy::{add, mul, parse_verbose_flag};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let verbose = parse_verbose_flag(&args[1..]);

    if args.len() < 4 {
        eprintln!("usage: mini-calc <add|mul> <a> <b>");
        std::process::exit(1);
    }

    let op = args[1].as_str();
    let a: i32 = args[2].parse().expect("a must be integer");
    let b: i32 = args[3].parse().expect("b must be integer");

    let result = match op {
        "add" => add(a, b),
        "mul" => mul(a, b),
        _ => {
            eprintln!("unknown op: {op}");
            std::process::exit(1);
        }
    };

    if verbose {
        eprintln!("verbose: {op} {a} {b} -> {result}");
    }

    println!("{result}");
}
